"""
Async client for the MLB Stats API (https://statsapi.mlb.com/api/v1/).

Design decisions:
- Token-bucket rate limiter keeps requests ≤ 10/s (MLB's undocumented soft limit).
- Every request is retried up to 3 times with exponential back-off on network
  errors and 5xx / 429 responses.
- All public methods return typed Python dicts — callers never touch raw JSON.
"""

import asyncio
import logging
import time
from datetime import date
from typing import TypedDict

import httpx

logger = logging.getLogger(__name__)


# ── Typed return shapes ────────────────────────────────────────────────────────


class GameInfo(TypedDict):
    game_id: int
    date: str
    home_team: str
    home_team_id: int
    away_team: str
    away_team_id: int
    home_score: int | None
    away_score: int | None
    status: str


class BattingStatsInfo(TypedDict):
    player_id: int
    player_name: str
    team: str
    team_id: int
    position: str
    at_bats: int
    hits: int
    home_runs: int
    rbis: int
    batting_avg: float | None
    on_base_pct: float | None
    slugging_pct: float | None


class PlayerInfo(TypedDict):
    player_id: int
    full_name: str
    team: str
    team_id: int
    position: str
    bats: str | None  # 'L' | 'R' | 'S' — batSide.code
    throws: str | None  # 'L' | 'R' — pitchHand.code


class PitchingLineInfo(TypedDict):
    """Per-game pitching line — what get_game_pitching_lines returns."""
    player_id: int
    player_name: str
    team: str
    team_id: int
    innings_pitched: float
    hits_allowed: int
    earned_runs: int
    walks_allowed: int
    strikeouts: int
    era: float | None
    whip: float | None
    throws: str | None  # 'L' | 'R' if available


class ProbablePitcherInfo(TypedDict):
    """Probable starting pitchers for one upcoming game."""
    game_id: int            # MLB gamePk
    date: str               # ISO YYYY-MM-DD
    home_team: str
    away_team: str
    home_pitcher_id: int | None
    home_pitcher_name: str | None
    away_pitcher_id: int | None
    away_pitcher_name: str | None


class BatterVsPitcherStats(TypedDict):
    """Career head-to-head batting line for one batter vs one pitcher."""
    batter_id: int
    pitcher_id: int
    at_bats: int
    hits: int
    home_runs: int
    batting_avg: float | None
    on_base_pct: float | None
    slugging_pct: float | None
    strikeouts: int


class PitcherRecentGame(TypedDict):
    """One game-log line from a pitcher's recent-form window."""
    date: str
    opponent: str
    innings_pitched: float
    hits_allowed: int
    earned_runs: int
    walks: int
    strikeouts: int
    era: float | None
    whip: float | None


class PitcherRecentForm(TypedDict):
    """A pitcher's last-N-games breakdown plus aggregates over that window."""
    pitcher_id: int
    num_games: int
    games: list[PitcherRecentGame]
    recent_era: float | None
    recent_whip: float | None
    recent_batting_avg_against: float | None


class RosterEntry(TypedDict):
    player_id: int
    full_name: str
    position: str
    jersey_number: str | None
    status: str


class TeamStanding(TypedDict):
    team_id: int
    team_name: str
    wins: int
    losses: int
    pct: float
    games_back: str
    league: str
    division: str


# ── Rate limiter ───────────────────────────────────────────────────────────────


class _TokenBucketRateLimiter:
    """
    Token-bucket rate limiter.

    Allows ``max_rate`` requests per ``period`` seconds.
    Acquiring a token sleeps the caller just long enough to stay within the
    limit — no requests are ever dropped.
    """

    def __init__(self, max_rate: int = 10, period: float = 1.0) -> None:
        self._rate = max_rate
        self._period = period
        self._tokens: float = float(max_rate)
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                self._rate,
                self._tokens + elapsed * self._rate / self._period,
            )
            self._last_refill = now

            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) * self._period / self._rate
                logger.debug("Rate limit: sleeping %.3fs", wait)
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


# ── MLB Client ────────────────────────────────────────────────────────────────


class MLBClient:
    """
    Async context-manager client for the MLB Stats API.

    Usage::

        async with MLBClient() as mlb:
            games = await mlb.get_todays_schedule()
    """

    BASE_URL = "https://statsapi.mlb.com/api/v1"
    _MAX_RETRIES = 3
    _RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._limiter = _TokenBucketRateLimiter(max_rate=10, period=1.0)

    async def __aenter__(self) -> "MLBClient":
        self._http = httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0),
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _get(self, path: str, params: dict | None = None) -> dict:
        """
        Rate-limited GET with exponential-backoff retry.

        Retries on network errors and :attr:`_RETRYABLE_STATUS` codes.
        Raises :exc:`httpx.HTTPStatusError` after exhausting retries.
        """
        assert self._http is not None, "Use MLBClient as an async context manager"

        last_exc: Exception | None = None
        for attempt in range(self._MAX_RETRIES):
            await self._limiter.acquire()
            try:
                resp = await self._http.get(path, params=params)
                if resp.status_code in self._RETRYABLE_STATUS:
                    wait = 2**attempt
                    logger.warning(
                        "MLB API %s → %d; retry %d/%d in %ds",
                        path,
                        resp.status_code,
                        attempt + 1,
                        self._MAX_RETRIES,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.TransportError as exc:
                wait = 2**attempt
                logger.warning(
                    "MLB API transport error on %s (%s); retry %d/%d in %ds",
                    path,
                    exc,
                    attempt + 1,
                    self._MAX_RETRIES,
                    wait,
                )
                last_exc = exc
                await asyncio.sleep(wait)

        raise RuntimeError(
            f"MLB API request failed after {self._MAX_RETRIES} attempts: {path}"
        ) from last_exc

    @staticmethod
    def _safe_float(value: str | float | None) -> float | None:
        """Parse a stat string like '.345' into a float, return None on failure."""
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    # ── Public API methods ────────────────────────────────────────────────────

    async def get_todays_schedule(self, target_date: date | None = None) -> list[GameInfo]:
        """
        Return the MLB schedule for *target_date* (defaults to today).

        Calls ``GET /schedule?sportId=1&date={date}``.
        """
        query_date = (target_date or date.today()).isoformat()
        data = await self._get("/schedule", params={"sportId": "1", "date": query_date})

        games: list[GameInfo] = []
        for date_block in data.get("dates", []):
            for g in date_block.get("games", []):
                home = g["teams"]["home"]
                away = g["teams"]["away"]
                games.append(
                    GameInfo(
                        game_id=g["gamePk"],
                        date=date_block["date"],
                        home_team=home["team"]["name"],
                        home_team_id=home["team"]["id"],
                        away_team=away["team"]["name"],
                        away_team_id=away["team"]["id"],
                        home_score=home.get("score"),
                        away_score=away.get("score"),
                        status=g["status"].get("detailedState", "Unknown"),
                    )
                )
        logger.info("get_todays_schedule: %d games on %s", len(games), query_date)
        return games

    async def get_game_boxscore(self, game_id: int) -> list[BattingStatsInfo]:
        """
        Return per-player batting stats for a completed game.

        Calls ``GET /game/{game_id}/boxscore``.
        Only players who batted (appear in the ``batters`` list) are returned.
        """
        data = await self._get(f"/game/{game_id}/boxscore")

        stats: list[BattingStatsInfo] = []
        for side in ("home", "away"):
            team_block = data.get("teams", {}).get(side, {})
            team_name: str = team_block.get("team", {}).get("name", "Unknown")
            team_id: int = team_block.get("team", {}).get("id", 0)
            batters: list[int] = team_block.get("batters", [])
            players: dict = team_block.get("players", {})

            for player_id in batters:
                key = f"ID{player_id}"
                player_data = players.get(key, {})
                batting = player_data.get("stats", {}).get("batting", {})
                if not batting:
                    continue  # skip pitchers / non-batters with no stats
                person = player_data.get("person", {})
                position = player_data.get("position", {}).get("abbreviation", "N/A")
                stats.append(
                    BattingStatsInfo(
                        player_id=player_id,
                        player_name=person.get("fullName", "Unknown"),
                        team=team_name,
                        team_id=team_id,
                        position=position,
                        at_bats=int(batting.get("atBats", 0)),
                        hits=int(batting.get("hits", 0)),
                        home_runs=int(batting.get("homeRuns", 0)),
                        rbis=int(batting.get("rbi", 0)),
                        batting_avg=self._safe_float(batting.get("avg")),
                        on_base_pct=self._safe_float(batting.get("obp")),
                        slugging_pct=self._safe_float(batting.get("slg")),
                    )
                )
        logger.info("get_game_boxscore(%d): %d player stat lines", game_id, len(stats))
        return stats

    async def get_player(self, player_id: int) -> PlayerInfo | None:
        """
        Return bio info for a single player.

        Calls ``GET /people/{player_id}``.
        Returns ``None`` if the player is not found.
        """
        try:
            data = await self._get(f"/people/{player_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.warning("get_player: player %d not found", player_id)
                return None
            raise

        people = data.get("people", [])
        if not people:
            return None

        p = people[0]
        team = p.get("currentTeam") or {}
        position = p.get("primaryPosition") or {}
        bat_side = (p.get("batSide") or {}).get("code")
        pitch_hand = (p.get("pitchHand") or {}).get("code")
        return PlayerInfo(
            player_id=p["id"],
            full_name=p.get("fullName", "Unknown"),
            team=team.get("name", "Unknown"),
            team_id=team.get("id", 0),
            position=position.get("abbreviation", "N/A"),
            bats=bat_side if bat_side in {"L", "R", "S"} else None,
            throws=pitch_hand if pitch_hand in {"L", "R"} else None,
        )

    # ── Pitching: boxscore lines + probable starters ────────────────────────

    async def get_game_pitching_lines(self, game_id: int) -> list[PitchingLineInfo]:
        """
        Per-pitcher line for a completed game (or partial for an
        in-progress game). Calls the same /boxscore endpoint as
        get_game_boxscore so a daily ETL run that touches both burns
        only one upstream request per game (the limiter / cache layer
        sees identical URLs).
        """
        data = await self._get(f"/game/{game_id}/boxscore")

        lines: list[PitchingLineInfo] = []
        for side in ("home", "away"):
            team_block = data.get("teams", {}).get(side, {})
            team_name: str = team_block.get("team", {}).get("name", "Unknown")
            team_id: int = team_block.get("team", {}).get("id", 0)
            pitchers: list[int] = team_block.get("pitchers", [])
            players: dict = team_block.get("players", {})

            for player_id in pitchers:
                key = f"ID{player_id}"
                player_data = players.get(key, {})
                pitching = player_data.get("stats", {}).get("pitching", {})
                if not pitching:
                    continue
                person = player_data.get("person", {})
                pitch_hand = (person.get("pitchHand") or {}).get("code")
                lines.append(
                    PitchingLineInfo(
                        player_id=player_id,
                        player_name=person.get("fullName", "Unknown"),
                        team=team_name,
                        team_id=team_id,
                        innings_pitched=self._parse_innings(pitching.get("inningsPitched")),
                        hits_allowed=int(pitching.get("hits", 0)),
                        earned_runs=int(pitching.get("earnedRuns", 0)),
                        walks_allowed=int(pitching.get("baseOnBalls", 0)),
                        strikeouts=int(pitching.get("strikeOuts", 0)),
                        era=self._safe_float(pitching.get("era")),
                        whip=self._safe_float(pitching.get("whip")),
                        throws=pitch_hand if pitch_hand in {"L", "R"} else None,
                    )
                )
        logger.info("get_game_pitching_lines(%d): %d pitchers", game_id, len(lines))
        return lines

    async def get_probable_pitchers(
        self, target_date: date | None = None
    ) -> list[ProbablePitcherInfo]:
        """
        Probable starting pitchers for every scheduled game on
        ``target_date`` (default: today). Returns one entry per game;
        missing starters surface as None so callers can decide whether
        to skip or wait for the announcement.

        Calls ``GET /schedule?sportId=1&date=YYYY-MM-DD&hydrate=probablePitcher``.
        """
        d = target_date or date.today()
        params = {
            "sportId": 1,
            "date": d.isoformat(),
            "hydrate": "probablePitcher",
        }
        data = await self._get("/schedule", params=params)

        results: list[ProbablePitcherInfo] = []
        for day in data.get("dates", []):
            for g in day.get("games", []):
                home = g.get("teams", {}).get("home", {})
                away = g.get("teams", {}).get("away", {})
                hp = home.get("probablePitcher") or {}
                ap = away.get("probablePitcher") or {}
                results.append(
                    ProbablePitcherInfo(
                        game_id=g.get("gamePk", 0),
                        date=g.get("gameDate", "")[:10] or d.isoformat(),
                        home_team=home.get("team", {}).get("name", "Unknown"),
                        away_team=away.get("team", {}).get("name", "Unknown"),
                        home_pitcher_id=hp.get("id"),
                        home_pitcher_name=hp.get("fullName"),
                        away_pitcher_id=ap.get("id"),
                        away_pitcher_name=ap.get("fullName"),
                    )
                )
        logger.info(
            "get_probable_pitchers(%s): %d games", d, len(results)
        )
        return results

    # ── Matchup factors: H2H history + recent pitcher form ──────────────────

    async def get_batter_vs_pitcher(
        self, batter_id: int, pitcher_id: int
    ) -> BatterVsPitcherStats | None:
        """
        Career head-to-head batting line for *batter_id* against *pitcher_id*.

        Calls ``GET /people/{batter_id}/stats
        ?stats=vsPlayer,vsPlayerTotal&opposingPlayerId={pitcher_id}&group=hitting``.

        Reads the ``vsPlayerTotal`` split — MLB's pre-aggregated career line
        across every season the pair has faced each other — rather than the
        per-season ``vsPlayer`` splits, because rate stats (avg/obp/slg)
        cannot be correctly recombined from season splits by hand.

        Returns ``None`` when:
          - they have never faced each other (200 with no splits, or 0 AB), or
          - a player id is unknown to the API (404 — common for minor leaguers).

        Rate limiting is handled by the shared token-bucket limiter (≤10/s),
        so no per-call sleep is added here even when called once per batter.
        """
        params = {
            "stats": "vsPlayer,vsPlayerTotal",
            "opposingPlayerId": pitcher_id,
            "group": "hitting",
        }
        try:
            data = await self._get(f"/people/{batter_id}/stats", params=params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.warning(
                    "get_batter_vs_pitcher: batter %d / pitcher %d not found (404)",
                    batter_id,
                    pitcher_id,
                )
                return None
            raise

        # Prefer the single pre-aggregated career-total split.
        total_split: dict | None = None
        for block in data.get("stats", []):
            if block.get("type", {}).get("displayName") == "vsPlayerTotal":
                splits = block.get("splits", [])
                if splits:
                    total_split = splits[0]
                    break

        if total_split is None:
            logger.info(
                "get_batter_vs_pitcher(%d vs %d): no head-to-head history",
                batter_id,
                pitcher_id,
            )
            return None

        stat = total_split.get("stat", {})
        at_bats = int(stat.get("atBats", 0))
        if at_bats == 0:
            # Only walks / HBP on record — no usable batting sample.
            return None

        return BatterVsPitcherStats(
            batter_id=batter_id,
            pitcher_id=pitcher_id,
            at_bats=at_bats,
            hits=int(stat.get("hits", 0)),
            home_runs=int(stat.get("homeRuns", 0)),
            batting_avg=self._safe_float(stat.get("avg")),
            on_base_pct=self._safe_float(stat.get("obp")),
            slugging_pct=self._safe_float(stat.get("slg")),
            strikeouts=int(stat.get("strikeOuts", 0)),
        )

    async def get_pitcher_recent_games(
        self, pitcher_id: int, num_games: int = 3, season: int | None = None
    ) -> PitcherRecentForm | None:
        """
        Recent-form pitching stats: *pitcher_id*'s most recent ``num_games``
        game-log entries for *season* (default: current year), plus aggregates
        across that window.

        Calls ``GET /people/{pitcher_id}/stats
        ?stats=gameLog&group=pitching&season={season}``.

        Per-game ``era`` / ``whip`` are recomputed from parsed innings (MLB
        encodes '6.1' as 6⅓) so they stay consistent with the aggregates:

          - ``recent_era``  = Σ earned_runs / Σ innings × 9
          - ``recent_whip`` = Σ (walks + hits) / Σ innings
          - ``recent_batting_avg_against`` = Σ hits / Σ batters_faced
            (falls back to hits / (innings×3 + hits + walks) when the game
            log omits ``battersFaced``)

        Returns ``None`` when the pitcher has no game log for the season, or
        on a 404. If fewer than ``num_games`` starts exist, uses all of them.

        Rate limiting is handled by the shared token-bucket limiter (≤10/s).
        """
        yr = season or date.today().year
        params = {"stats": "gameLog", "group": "pitching", "season": yr}
        try:
            data = await self._get(f"/people/{pitcher_id}/stats", params=params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.warning(
                    "get_pitcher_recent_games: pitcher %d not found (404)", pitcher_id
                )
                return None
            raise

        stats_blocks = data.get("stats", [])
        splits = stats_blocks[0].get("splits", []) if stats_blocks else []
        if not splits:
            logger.info(
                "get_pitcher_recent_games(%d): no game log for season %s",
                pitcher_id,
                yr,
            )
            return None

        # Game log comes chronological ascending; sort by date desc, take N.
        splits_sorted = sorted(splits, key=lambda s: s.get("date", ""), reverse=True)
        recent = splits_sorted[:num_games]

        games: list[PitcherRecentGame] = []
        tot_ip = 0.0
        tot_er = 0
        tot_bb = 0
        tot_hits = 0
        tot_bf = 0
        for sp in recent:
            st = sp.get("stat", {})
            ip = self._parse_innings(st.get("inningsPitched"))
            hits = int(st.get("hits", 0))
            er = int(st.get("earnedRuns", 0))
            bb = int(st.get("baseOnBalls", 0))
            k = int(st.get("strikeOuts", 0))
            bf = int(st.get("battersFaced", 0))
            games.append(
                PitcherRecentGame(
                    date=sp.get("date", ""),
                    opponent=(sp.get("opponent") or {}).get("name", "Unknown"),
                    innings_pitched=round(ip, 3),
                    hits_allowed=hits,
                    earned_runs=er,
                    walks=bb,
                    strikeouts=k,
                    era=round(er / ip * 9, 2) if ip > 0 else None,
                    whip=round((bb + hits) / ip, 2) if ip > 0 else None,
                )
            )
            tot_ip += ip
            tot_er += er
            tot_bb += bb
            tot_hits += hits
            tot_bf += bf

        recent_era = round(tot_er / tot_ip * 9, 2) if tot_ip > 0 else None
        recent_whip = round((tot_bb + tot_hits) / tot_ip, 2) if tot_ip > 0 else None
        if tot_bf > 0:
            recent_baa: float | None = round(tot_hits / tot_bf, 3)
        elif tot_ip > 0:
            approx_bf = tot_ip * 3 + tot_hits + tot_bb
            recent_baa = round(tot_hits / approx_bf, 3) if approx_bf > 0 else None
        else:
            recent_baa = None

        logger.info(
            "get_pitcher_recent_games(%d): %d games, recent_era=%s recent_whip=%s",
            pitcher_id,
            len(games),
            recent_era,
            recent_whip,
        )
        return PitcherRecentForm(
            pitcher_id=pitcher_id,
            num_games=len(games),
            games=games,
            recent_era=recent_era,
            recent_whip=recent_whip,
            recent_batting_avg_against=recent_baa,
        )

    @staticmethod
    def _parse_innings(raw: object) -> float:
        """MLB returns innings as a quirky string: '6.1' means 6⅓
        innings, '6.2' = 6⅔. Convert to standard decimal float."""
        if raw is None:
            return 0.0
        try:
            s = str(raw)
            if "." in s:
                whole, frac = s.split(".", 1)
                return int(whole) + {"0": 0.0, "1": 1 / 3, "2": 2 / 3}.get(frac, 0.0)
            return float(s)
        except (ValueError, TypeError):
            return 0.0

    async def get_team_roster(self, team_id: int) -> list[RosterEntry]:
        """
        Return the active roster for a team.

        Calls ``GET /teams/{team_id}/roster``.
        """
        data = await self._get(f"/teams/{team_id}/roster")

        roster: list[RosterEntry] = []
        for entry in data.get("roster", []):
            person = entry.get("person", {})
            position = entry.get("position", {})
            roster.append(
                RosterEntry(
                    player_id=person.get("id", 0),
                    full_name=person.get("fullName", "Unknown"),
                    position=position.get("abbreviation", "N/A"),
                    jersey_number=entry.get("jerseyNumber"),
                    status=entry.get("status", {}).get("description", "Unknown"),
                )
            )
        logger.info("get_team_roster(%d): %d players", team_id, len(roster))
        return roster

    async def get_standings(self) -> list[TeamStanding]:
        """
        Return current standings for both leagues (AL + NL).

        Calls ``GET /standings?leagueId=103,104``.
        """
        data = await self._get("/standings", params={"leagueId": "103,104"})

        standings: list[TeamStanding] = []
        for record in data.get("records", []):
            league_name: str = record.get("league", {}).get("name", "Unknown")
            division_name: str = record.get("division", {}).get("name", "Unknown")
            for tr in record.get("teamRecords", []):
                team = tr.get("team", {})
                standings.append(
                    TeamStanding(
                        team_id=team.get("id", 0),
                        team_name=team.get("name", "Unknown"),
                        wins=int(tr.get("wins", 0)),
                        losses=int(tr.get("losses", 0)),
                        pct=self._safe_float(tr.get("winningPercentage")) or 0.0,
                        games_back=str(tr.get("gamesBack", "-")),
                        league=league_name,
                        division=division_name,
                    )
                )
        logger.info("get_standings: %d team records", len(standings))
        return standings
