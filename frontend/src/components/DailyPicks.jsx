/**
 * DailyPicks — high-confidence hit-probability picks for today's slate.
 *
 * - Header shows the date + a 30-day model-accuracy badge.
 * - Picks render as expandable cards: probability ring, confidence
 *   badge, opposing pitcher, expand to see the full factor breakdown
 *   and the game's current odds.
 * - Sort toggle (probability / confidence) and a min-confidence slider
 *   refine the visible set client-side so we don't refetch.
 * - Empty states cover both "no games today" and "no players meet
 *   the threshold".
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  PolarAngleAxis,
  RadialBar,
  RadialBarChart,
  ResponsiveContainer,
} from 'recharts'
import api from '../api/client'

const fmtAvg = (v) => (v == null ? '—' : v.toFixed(3).replace(/^0/, ''))
const fmtFloat = (v, decimals = 2) =>
  v == null ? '—' : Number(v).toFixed(decimals)

const CONF_TIER = (n) => (n >= 80 ? 'high' : n >= 50 ? 'medium' : 'low')
const CONF_STYLE = {
  high:   'bg-emerald-500/20 text-emerald-300 ring-emerald-500/30',
  medium: 'bg-amber-500/20 text-amber-300 ring-amber-500/30',
  low:    'bg-slate-500/20 text-slate-300 ring-slate-500/30',
}

const ringColor = (p) => {
  if (p >= 0.85) return '#10b981'
  if (p >= 0.75) return '#3b82f6'
  if (p >= 0.65) return '#f59e0b'
  return '#ef4444'
}

const handednessLabel = (mod) => {
  if (mod == null || mod === 0) return { text: 'Unknown', sigil: '·', tone: 'text-slate-400' }
  if (mod > 0) return { text: 'Favorable matchup', sigil: '✓', tone: 'text-emerald-300' }
  return { text: 'Same-hand penalty', sigil: '✗', tone: 'text-red-300' }
}

export default function DailyPicks() {
  const [sortBy, setSortBy] = useState('probability')
  const [minConf, setMinConf] = useState(50)
  const [expanded, setExpanded] = useState(new Set())

  const picksQuery = useQuery({
    queryKey: ['picks-today'],
    queryFn: async () => {
      const { data } = await api.get('/picks/today')
      return data
    },
    staleTime: 60_000,
  })

  const accuracyQuery = useQuery({
    queryKey: ['picks-accuracy', 30],
    queryFn: async () => {
      const { data } = await api.get('/picks/accuracy?days=30')
      return data
    },
    staleTime: 5 * 60_000,
  })

  const today = new Date().toLocaleDateString(undefined, {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
    year: 'numeric',
  })

  const sortedPicks = useMemo(() => {
    const picks = picksQuery.data?.picks ?? []
    const filtered = picks.filter((p) => p.confidence >= minConf)
    return filtered.sort((a, b) =>
      sortBy === 'probability'
        ? b.probability - a.probability
        : b.confidence - a.confidence,
    )
  }, [picksQuery.data, sortBy, minConf])

  const toggleExpanded = (id) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  return (
    <section className="space-y-4">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-xl font-semibold text-white">Daily Picks</h2>
          <p className="text-sm text-slate-400">{today}</p>
        </div>
        <AccuracyBadge data={accuracyQuery.data} loading={accuracyQuery.isPending} />
      </header>

      {/* Controls */}
      <div className="flex flex-col gap-3 rounded-lg border border-slate-800 bg-slate-900/60 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-2 text-sm">
          <span className="text-slate-400">Sort:</span>
          {['probability', 'confidence'].map((opt) => (
            <button
              key={opt}
              onClick={() => setSortBy(opt)}
              className={[
                'rounded-md px-3 py-1 text-xs font-medium transition-colors',
                sortBy === opt
                  ? 'bg-blue-600 text-white'
                  : 'bg-slate-800 text-slate-300 hover:bg-slate-700',
              ].join(' ')}
            >
              {opt}
            </button>
          ))}
        </div>
        <label className="flex items-center gap-3 text-sm">
          <span className="text-slate-400">Min confidence:</span>
          <input
            type="range"
            min={0}
            max={100}
            step={5}
            value={minConf}
            onChange={(e) => setMinConf(Number(e.target.value))}
            className="w-40 accent-blue-500"
          />
          <span className="w-10 text-right font-medium text-slate-200 tabular-nums">
            {minConf}
          </span>
        </label>
      </div>

      {/* Body */}
      {picksQuery.isPending && <CardSkeletons />}

      {picksQuery.isError && (
        <ErrorPanel
          title="Couldn't load today's picks"
          message={
            picksQuery.error?.response?.data?.detail || picksQuery.error.message
          }
        />
      )}

      {!picksQuery.isPending && !picksQuery.isError && (
        <PicksBody
          data={picksQuery.data}
          picks={sortedPicks}
          minConf={minConf}
          expanded={expanded}
          onToggle={toggleExpanded}
        />
      )}
    </section>
  )
}

// ── Body / states ───────────────────────────────────────────────────────────

function PicksBody({ data, picks, minConf, expanded, onToggle }) {
  const noGames = (data?.games_considered ?? 0) === 0
  const noPicks = data?.pick_count === 0
  const filteredOutAll = data?.pick_count > 0 && picks.length === 0

  if (noGames) {
    return (
      <EmptyPanel
        title="No games today"
        subtitle="Picks populate when MLB has games scheduled for the date."
      />
    )
  }

  if (noPicks) {
    return (
      <EmptyPanel
        title="Lineups haven't dropped yet"
        subtitle="Starting lineups haven't been announced yet. Picks are generated once lineups are confirmed, typically 1–2 hours before first pitch."
      />
    )
  }

  if (filteredOutAll) {
    return (
      <EmptyPanel
        title="Nothing meets the current confidence floor"
        subtitle={`${data.pick_count} pick${data.pick_count === 1 ? '' : 's'} ranked above ${(data.min_probability * 100).toFixed(0)}% probability, but none have confidence ≥ ${minConf}. Try lowering the slider.`}
      />
    )
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        Showing {picks.length} of {data.pick_count} pick{data.pick_count === 1 ? '' : 's'} ·{' '}
        {data.candidates_evaluated} candidate batters evaluated across {data.games_considered} game{data.games_considered === 1 ? '' : 's'}
      </p>
      {picks.map((p) => (
        <PickCard
          key={`${p.player_id}-${p.game_id}`}
          pick={p}
          expanded={expanded.has(`${p.player_id}-${p.game_id}`)}
          onToggle={() => onToggle(`${p.player_id}-${p.game_id}`)}
        />
      ))}
    </div>
  )
}

// ── Pick card ───────────────────────────────────────────────────────────────

function PickCard({ pick, expanded, onToggle }) {
  const tier = CONF_TIER(pick.confidence)
  const pct = Math.round(pick.probability * 1000) / 10
  const ringData = [{ name: 'p', value: pct, fill: ringColor(pick.probability) }]

  return (
    <article className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900/60">
      <button
        onClick={onToggle}
        className="flex w-full items-center gap-4 px-4 py-4 text-left transition-colors hover:bg-slate-800/40"
      >
        <div className="relative h-16 w-16 shrink-0">
          <ResponsiveContainer width="100%" height="100%">
            <RadialBarChart
              innerRadius="60%"
              outerRadius="100%"
              data={ringData}
              startAngle={90}
              endAngle={-270}
            >
              <PolarAngleAxis type="number" domain={[0, 100]} angleAxisId={0} tick={false} />
              <RadialBar
                background={{ fill: '#1e293b' }}
                dataKey="value"
                cornerRadius={20}
                isAnimationActive={false}
              />
            </RadialBarChart>
          </ResponsiveContainer>
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-sm font-semibold text-white tabular-nums">
            {pct.toFixed(1)}%
          </div>
        </div>

        <div className="flex-1 min-w-0">
          <p className="truncate font-semibold text-white">{pick.player_name}</p>
          <p className="text-xs text-slate-400">
            {pick.team} vs {pick.opponent}
            {pick.pitcher_name && (
              <>
                {' '}· P: <span className="text-slate-300">{pick.pitcher_name}</span>
              </>
            )}
          </p>
        </div>

        <span
          className={[
            'rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 capitalize',
            CONF_STYLE[tier],
          ].join(' ')}
        >
          {tier} ({pick.confidence})
        </span>
        <span className="text-slate-500">{expanded ? '▾' : '▸'}</span>
      </button>

      {expanded && (
        <div className="border-t border-slate-800 bg-slate-950/40 px-4 py-4">
          <PickDetails pick={pick} />
        </div>
      )}
    </article>
  )
}

function PickDetails({ pick }) {
  const f = pick.factors || {}
  const hand = handednessLabel(f.handedness_matchup)

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div>
        <p className="mb-2 text-xs font-medium uppercase tracking-wider text-slate-400">
          Batter factors
        </p>
        <dl className="space-y-1.5 text-sm">
          <Row label="Recent avg (last 15 games)" value={fmtAvg(f.recent_avg)} />
          <Row label="Season avg" value={fmtAvg(f.season_avg)} />
          <Row label="Career avg" value={fmtAvg(f.career_avg)} />
          <Row label="Home/away split" value={fmtAvg(f.home_away_split)} />
        </dl>

        <p className="mt-3 mb-2 text-xs font-medium uppercase tracking-wider text-slate-400">
          Matchup
        </p>
        <dl className="space-y-1.5 text-sm">
          <Row label="Opposing pitcher ERA" value={fmtFloat(f.pitcher_era, 2)} />
          <Row label="Opposing pitcher WHIP" value={fmtFloat(f.pitcher_whip, 2)} />
          <Row
            label="Handedness"
            value={
              <span className={hand.tone}>
                {hand.text} <span className="ml-1">{hand.sigil}</span>
              </span>
            }
          />
          <Row label="League avg baseline" value={fmtAvg(f.league_avg)} />
        </dl>
      </div>

      <div>
        <p className="mb-2 text-xs font-medium uppercase tracking-wider text-slate-400">
          Live odds for {pick.team} @ {pick.opponent}
        </p>
        <OddsForPick odds={pick.odds} />
      </div>
    </div>
  )
}

function OddsForPick({ odds }) {
  const books = odds ? Object.values(odds) : []
  if (books.length === 0) {
    return (
      <p className="rounded-md bg-slate-800/40 px-3 py-2 text-xs text-slate-500">
        No odds published for this game yet.
      </p>
    )
  }
  return (
    <div className="overflow-hidden rounded-md border border-slate-800">
      <table className="min-w-full text-xs">
        <thead className="bg-slate-900/80 text-slate-400">
          <tr>
            <th className="px-2 py-1.5 text-left font-medium">Book</th>
            <th className="px-2 py-1.5 text-right font-medium">Home ML</th>
            <th className="px-2 py-1.5 text-right font-medium">Away ML</th>
            <th className="px-2 py-1.5 text-right font-medium">O/U</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800/60 text-slate-200">
          {books.map((b) => (
            <tr key={b.sportsbook}>
              <td className="px-2 py-1.5 font-medium text-slate-300">{b.sportsbook}</td>
              <td className="px-2 py-1.5 text-right tabular-nums">
                {b.home_moneyline == null ? '—' : b.home_moneyline > 0 ? `+${b.home_moneyline}` : b.home_moneyline}
              </td>
              <td className="px-2 py-1.5 text-right tabular-nums">
                {b.away_moneyline == null ? '—' : b.away_moneyline > 0 ? `+${b.away_moneyline}` : b.away_moneyline}
              </td>
              <td className="px-2 py-1.5 text-right tabular-nums">{b.over_under ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Row({ label, value }) {
  return (
    <div className="flex items-baseline justify-between rounded-md bg-slate-900/40 px-2.5 py-1.5">
      <dt className="text-slate-400">{label}</dt>
      <dd className="font-medium text-slate-100 tabular-nums">{value}</dd>
    </div>
  )
}

function AccuracyBadge({ data, loading }) {
  if (loading) return <span className="text-xs text-slate-500">accuracy loading…</span>
  if (!data || data.total_picks === 0) {
    return (
      <span className="rounded-full bg-slate-800 px-3 py-1 text-xs text-slate-400 ring-1 ring-slate-700">
        No graded picks in the last 30 days yet
      </span>
    )
  }
  const pct = data.accuracy_pct
  const tone =
    pct >= 70 ? 'bg-emerald-500/20 text-emerald-300 ring-emerald-500/30'
    : pct >= 55 ? 'bg-blue-500/20 text-blue-300 ring-blue-500/30'
    : 'bg-amber-500/20 text-amber-300 ring-amber-500/30'
  const recent = data.recent_hits ?? []
  return (
    <div className="flex flex-col items-end gap-1.5">
      <span className={['rounded-full px-3 py-1 text-xs font-medium ring-1', tone].join(' ')}>
        {pct?.toFixed(1)}% accurate over last 30 days ({data.correct_predictions}/{data.total_picks})
      </span>
      {recent.length > 0 && (
        <div className="flex flex-wrap justify-end gap-1.5 max-w-[28rem]">
          <span className="text-[11px] uppercase tracking-wide text-slate-500 self-center">
            Recent hits:
          </span>
          {recent.map((h) => (
            <span
              key={`${h.player_id}-${h.game_date}`}
              title={`${h.player_name} — ${(h.predicted_probability * 100).toFixed(1)}% predicted on ${h.game_date}`}
              className="rounded-full bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-300 ring-1 ring-emerald-500/20"
            >
              {h.player_name}
              <span className="ml-1 text-emerald-200/60">
                {(h.predicted_probability * 100).toFixed(0)}%
              </span>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function CardSkeletons() {
  return (
    <div className="space-y-3">
      {Array.from({ length: 5 }).map((_, i) => (
        <div
          key={i}
          className="h-24 animate-pulse rounded-xl border border-slate-800 bg-slate-900/60"
        />
      ))}
    </div>
  )
}

function EmptyPanel({ title, subtitle }) {
  return (
    <div className="rounded-xl border border-dashed border-slate-700 bg-slate-900/40 p-10 text-center">
      <h3 className="text-base font-medium text-white">{title}</h3>
      <p className="mt-2 max-w-xl mx-auto text-sm text-slate-400">{subtitle}</p>
    </div>
  )
}

function ErrorPanel({ title, message }) {
  return (
    <div className="rounded-xl border border-red-500/30 bg-red-500/5 p-6">
      <h3 className="text-base font-medium text-red-200">{title}</h3>
      <p className="mt-1 text-sm text-red-300/80">{message}</p>
    </div>
  )
}
