// Babysit for Hermes Desktop: a pane, a full page and a status chip over the watches the Babysit
// plugin publishes. Read-only: every byte comes from GET /api/plugins/babysit/watches.
// Loaded uncompiled (no JSX), imports only the SDK and React, colors come from theme variables, and
// every piece of log text reaches the page as a React text child, never as markup.
import { host, PALETTE_AREA, PANES_AREA, ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS, useQuery } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'babysit'
const POLL_MS = 2000
const ALARMS = new Set(['nan', 'oom', 'traceback', 'memory-pressure'])
const KIND_LABEL = {
  nan: 'Non-finite value', oom: 'Out of memory', 'memory-pressure': 'Memory pressure', traceback: 'Traceback', stall: 'Stall', alert: 'Alert',
  'process ended': 'Process ended', 'watch expired': 'Watch expired', 'watch stopped': 'Watch stopped'
}
const KIND_GLYPH = { nan: 'N', oom: 'M', traceback: 'T', stall: 'S', alert: 'A', 'memory-pressure': 'R' }
const SERIES_LABEL = { rss_mb: 'memory MB' }
const CLOCKS = new Set(['elapsed', 'time', 'timestamp']) // wall-clock counters chart as a straight line and say nothing
const RING_RADIUS = 52
const RING_LENGTH = 2 * Math.PI * RING_RADIUS

let rest = null // ctx.rest, bound in register(): the components below live outside it

// ---------------------------------------------------------------- pure helpers (exported for tests)

export const h = (type, props, ...kids) => {
  const children = kids.length > 1 ? kids : kids[0]
  return kids.length > 1 ? jsxs(type, { ...props, children }) : jsx(type, { ...props, children })
}

/** Exponential moving average, seeded with the first value. alpha in (0, 1]. */
export function ema(values, alpha) {
  const out = []
  let prev = null
  for (const value of values) {
    prev = prev === null ? value : alpha * value + (1 - alpha) * prev
    out.push(prev)
  }
  return out
}

/** Round tick values inside [min, max], about `count` of them. */
export function niceTicks(min, max, count = 4) {
  if (!(max > min)) return [min]
  const raw = (max - min) / count
  const mag = 10 ** Math.floor(Math.log10(raw))
  const norm = raw / mag
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag
  const ticks = []
  for (let v = Math.ceil(min / step) * step, i = 0; v <= max + step * 1e-9 && i < 50; v += step, i++) {
    ticks.push(Number(v.toPrecision(12)))
  }
  return ticks
}

/** Index of the point whose x is nearest to `x`; points are [x, t, value]. */
export function nearest(points, x) {
  let best = 0
  for (let i = 1; i < points.length; i++) {
    if (Math.abs(points[i][0] - x) < Math.abs(points[best][0] - x)) best = i
  }
  return best
}

export function fmtNum(value) {
  if (value === null || value === undefined || !Number.isFinite(value)) return '–'
  if (value !== 0 && Math.abs(value) < 1e-3) return value.toExponential(0).replace('e-0', 'e-').replace('e+', 'e')
  return Number(value.toPrecision(3)).toString().replace('e+', 'e')
}

export function fmtBytes(value) {
  if (!Number.isFinite(value)) return '–'
  const units = ['B', 'K', 'M', 'G', 'T']
  let v = value
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)}${units[i]}`
}

export function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '–'
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return s % 60 === 0 ? `${m}m` : `${m}m${String(s % 60).padStart(2, '0')}s`
  const hrs = Math.floor(m / 60)
  return hrs < 48 ? `${hrs}h${String(m % 60).padStart(2, '0')}m` : `${Math.floor(hrs / 24)}d${hrs % 24}h`
}

const clock = seconds => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`

/** The card's mood: alarm (a failure kind just happened), warn (stall or lost), done, or live. */
export function toneOf(watch) {
  if (watch.state === 'lost') return 'warn'
  if (watch.state !== 'running') {
    return watch.events.some(e => ALARMS.has(e.kind)) || /killed|OOM|crashed|exited with code [1-9]/.test(watch.detail || '') ? 'alarm' : 'done'
  }
  const recent = watch.events.filter(e => watch.uptime - e.last < 30) // a job still repeating its failure stays an alarm
  if (recent.some(e => ALARMS.has(e.kind))) return 'alarm'
  if (recent.some(e => e.kind === 'stall')) return 'warn'
  return 'live'
}

/** The failure the card leads with: the first alarm event, else the final event of an ended watch. */
export function headline(watch) {
  const alarm = watch.events.find(e => ALARMS.has(e.kind))
  if (alarm) return alarm
  return watch.state === 'running' ? null : watch.events[watch.events.length - 1] || null
}

// ---------------------------------------------------------------- styles

const CSS = `
.bs-root{--bs-gap:16px;height:100%;overflow:auto;padding:20px 24px 32px;color:var(--ui-text-primary);font-size:13px;line-height:1.45;font-variant-numeric:tabular-nums}
.bs-root[data-pane=true]{padding:10px 10px 20px}
.bs-top{display:flex;align-items:center;gap:12px;margin:0 0 18px}
.bs-title{font-size:20px;font-weight:650;letter-spacing:-.01em}
.bs-root[data-pane=true] .bs-title{display:none}.bs-root[data-pane=true] .bs-top{margin-bottom:8px}
.bs-sub{color:var(--ui-text-tertiary)}
.bs-pill{display:inline-flex;align-items:center;gap:6px;height:22px;padding:0 9px;border-radius:999px;border:1px solid var(--ui-stroke-secondary);color:var(--ui-text-secondary);font-size:11.5px;background:var(--ui-bg-quaternary)}
.bs-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,760px),1fr));gap:20px}
.bs-root[data-pane=true] .bs-grid{grid-template-columns:minmax(0,1fr);gap:12px}
.bs-dot{width:8px;height:8px;border-radius:50%;background:var(--bs-tone,var(--ui-green));flex:none}
.bs-dot[data-pulse=true]{animation:bs-pulse 1.6s ease-out infinite}
@keyframes bs-pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--bs-tone,var(--ui-green)) 55%,transparent)}100%{box-shadow:0 0 0 9px transparent}}
.bs-card{container-type:inline-size;--bs-tone:var(--ui-green);position:relative;border:1px solid var(--ui-stroke-secondary);border-radius:16px;background:linear-gradient(180deg,color-mix(in srgb,var(--bs-tone) 5%,transparent),transparent 38%),var(--ui-bg-card);overflow:hidden;transition:border-color .4s,box-shadow .4s}
.bs-card[data-tone=live]{--bs-tone:var(--ui-green)}
.bs-card[data-tone=alarm]{--bs-tone:var(--ui-red);border-color:color-mix(in srgb,var(--ui-red) 55%,var(--ui-stroke-secondary));box-shadow:0 18px 60px -28px color-mix(in srgb,var(--ui-red) 70%,transparent)}
.bs-card[data-tone=warn]{--bs-tone:var(--ui-yellow);border-color:color-mix(in srgb,var(--ui-yellow) 45%,var(--ui-stroke-secondary))}
.bs-card[data-tone=done]{--bs-tone:var(--ui-text-tertiary)}
.bs-head{display:flex;align-items:center;gap:10px;padding:14px 18px 0;min-width:0}
.bs-name{font-size:15px;font-weight:620;white-space:nowrap}
.bs-path{color:var(--ui-text-tertiary);font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:1;font-family:var(--font-mono,ui-monospace,Menlo,monospace)}
.bs-state{color:var(--bs-tone);font-weight:560;font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;white-space:nowrap}
.bs-banner{display:flex;gap:10px;align-items:flex-start;margin:12px 18px 0;padding:9px 12px;border-radius:10px;border:1px solid color-mix(in srgb,var(--bs-tone) 40%,transparent);background:color-mix(in srgb,var(--bs-tone) 11%,transparent);animation:bs-rise .45s cubic-bezier(.2,.8,.2,1)}
.bs-banner b{color:var(--bs-tone);font-weight:620}
.bs-banner code{font-family:var(--font-mono,ui-monospace,Menlo,monospace);font-size:11.5px;color:var(--ui-text-secondary);word-break:break-word}
@keyframes bs-rise{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
.bs-body{display:grid;grid-template-columns:200px minmax(0,1fr) 272px;gap:22px;padding:16px 18px 6px;align-items:start}
.bs-ringcol{display:flex;flex-direction:column;align-items:center;gap:8px}
.bs-ring{width:176px;height:176px;position:relative}
.bs-ring svg{width:100%;height:100%;transform:rotate(-90deg)}
.bs-ring .track{fill:none;stroke:var(--ui-stroke-tertiary);stroke-width:9}
.bs-ring .arc{fill:none;stroke:var(--bs-tone);stroke-width:9;stroke-linecap:round;transition:stroke-dashoffset .9s cubic-bezier(.2,.8,.2,1),stroke .4s}
.bs-ring[data-indeterminate=true] .arc{stroke-dasharray:60 270;animation:bs-spin 2.4s linear infinite;transform-origin:60px 60px}
@keyframes bs-spin{to{transform:rotate(360deg)}}
.bs-ringtxt{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px}
.bs-pct{font-size:38px;font-weight:650;letter-spacing:-.03em;line-height:1}
.bs-pct small{font-size:17px;font-weight:560;color:var(--ui-text-tertiary);margin-left:1px}
.bs-step{font-size:11.5px;color:var(--ui-text-tertiary)}
.bs-eta{font-size:12.5px;color:var(--ui-text-secondary);text-align:center}
.bs-eta b{color:var(--ui-text-primary);font-weight:620}
.bs-stats{display:flex;flex-wrap:wrap;gap:6px;justify-content:center}
.bs-chart{min-width:0}
.bs-charthead{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}
.bs-chips{display:flex;gap:4px;flex-wrap:wrap;flex:1}
.bs-chip{height:24px;padding:0 9px;border-radius:7px;border:1px solid transparent;background:transparent;color:var(--ui-text-tertiary);font:inherit;font-size:12px;cursor:pointer}
.bs-chip:hover{background:var(--ui-control-hover-background);color:var(--ui-text-primary)}
.bs-chip[aria-pressed=true]{background:var(--ui-control-active-background);color:var(--ui-text-primary);border-color:var(--ui-stroke-secondary)}
.bs-switch{display:inline-flex;align-items:center;gap:7px;color:var(--ui-text-secondary);font-size:12px;cursor:pointer;user-select:none;background:none;border:0;font-family:inherit;padding:0}
.bs-switch i{width:26px;height:15px;border-radius:999px;background:var(--ui-bg-primary);position:relative;transition:background .2s}
.bs-switch i::after{content:"";position:absolute;top:2px;left:2px;width:11px;height:11px;border-radius:50%;background:var(--ui-text-secondary);transition:transform .2s,background .2s}
.bs-switch[aria-pressed=true] i{background:color-mix(in srgb,var(--ui-accent) 55%,transparent)}
.bs-switch[aria-pressed=true] i::after{transform:translateX(11px);background:var(--ui-text-primary)}
.bs-plot{position:relative;width:100%}
.bs-plot svg{display:block;width:100%;overflow:visible}
.bs-grid-line{stroke:var(--ui-stroke-tertiary);stroke-width:1}
.bs-axis{fill:var(--ui-text-quaternary);font-size:10.5px;font-family:var(--font-mono,ui-monospace,Menlo,monospace)}
.bs-raw{fill:none;stroke:var(--ui-accent);stroke-width:1.2;opacity:.34;stroke-linejoin:round}
.bs-line{fill:none;stroke:var(--ui-accent);stroke-width:2.4;stroke-linejoin:round;stroke-linecap:round}
.bs-area{opacity:.9}
.bs-zone{fill:color-mix(in srgb,var(--ui-red) 9%,transparent)}
.bs-zonelabel{fill:var(--ui-red);font-size:10.5px;font-weight:600;letter-spacing:.04em}
.bs-mk{--k:var(--ui-text-tertiary)}
.bs-mk[data-kind=nan],.bs-mk[data-kind=traceback]{--k:var(--ui-red)}
.bs-mk[data-kind=oom],.bs-mk[data-kind=memory-pressure]{--k:var(--ui-orange)}
.bs-mk[data-kind=stall]{--k:var(--ui-yellow)}
.bs-mk[data-kind=alert]{--k:var(--ui-purple)}
.bs-mk line{stroke:var(--k);stroke-width:1.2;stroke-dasharray:3 3;opacity:.8}
.bs-mk circle{fill:var(--ui-bg-editor);stroke:var(--k);stroke-width:1.6}
.bs-mk text{fill:var(--k);font-size:9px;font-weight:700;text-anchor:middle;dominant-baseline:central}
.bs-mk[data-new=true]{animation:bs-rise .5s}
.bs-endcap{fill:var(--ui-red);stroke:var(--ui-bg-editor);stroke-width:2}
.bs-cursor{stroke:var(--ui-text-tertiary);stroke-width:1;stroke-dasharray:2 3}
.bs-hoverdot{fill:var(--ui-bg-editor);stroke:var(--ui-accent);stroke-width:2.2}
.bs-tip{position:absolute;z-index:5;pointer-events:none;min-width:150px;max-width:260px;padding:8px 10px;border-radius:10px;border:1px solid var(--ui-stroke-secondary);background:color-mix(in srgb,var(--ui-bg-elevated) 96%,transparent);box-shadow:0 12px 32px -12px rgb(0 0 0 / .35);font-size:12px}
.bs-tip b{display:block;font-weight:620;margin-bottom:2px}
.bs-tip .row{display:flex;justify-content:space-between;gap:14px;color:var(--ui-text-secondary)}
.bs-tip .row span:last-child{color:var(--ui-text-primary);font-weight:560}
.bs-tip code{display:block;margin-top:4px;font-family:var(--font-mono,ui-monospace,Menlo,monospace);font-size:11px;color:var(--ui-text-tertiary);word-break:break-word}
.bs-side{display:flex;flex-direction:column;gap:14px;min-width:0}
.bs-facts{display:flex;flex-direction:column;gap:2px;margin-top:10px;color:var(--ui-text-tertiary);font-size:12px}
.bs-facts b{color:var(--ui-text-primary);font-weight:620}
.bs-label{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--ui-text-quaternary);margin-bottom:6px;display:flex;justify-content:space-between}
.bs-wakes{display:flex;align-items:baseline;gap:8px}
.bs-wakes strong{font-size:26px;font-weight:650;letter-spacing:-.02em}
.bs-mem{height:4px;border-radius:2px;background:var(--ui-bg-primary);margin-top:7px;overflow:hidden}
.bs-mem i{display:block;height:100%;border-radius:2px;background:var(--ui-accent)}
.bs-mem[data-hot=true] i{background:var(--ui-orange)}
.bs-ticks{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:3px;margin-top:7px}
.bs-ticks i{height:9px;border-radius:2px;background:var(--ui-bg-primary)}
.bs-ticks i[data-on=true]{background:var(--ui-accent)}
.bs-ticks i[data-last=true]{background:var(--ui-red)}
.bs-tl{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:2px;max-height:196px;overflow:auto}
.bs-tl li{display:grid;grid-template-columns:38px 10px minmax(0,1fr);gap:8px;align-items:baseline;padding:3px 0;animation:bs-rise .4s}
.bs-tl time{color:var(--ui-text-quaternary);font-family:var(--font-mono,ui-monospace,Menlo,monospace);font-size:11px}
.bs-tl .k{width:8px;height:8px;border-radius:50%;background:var(--k,var(--ui-text-tertiary));align-self:center}
.bs-tl li[data-kind=nan],.bs-tl li[data-kind=traceback]{--k:var(--ui-red)}
.bs-tl li[data-kind=oom],.bs-tl li[data-kind=memory-pressure]{--k:var(--ui-orange)}
.bs-tl li[data-kind=stall]{--k:var(--ui-yellow)}
.bs-tl li[data-kind=alert]{--k:var(--ui-purple)}
.bs-tl .t{min-width:0;color:var(--ui-text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bs-tl .t b{color:var(--ui-text-primary);font-weight:560;margin-right:6px}
.bs-tail{margin:12px 18px 16px;border-radius:10px;border:1px solid var(--ui-stroke-tertiary);background:var(--ui-bg-quaternary);padding:8px 0;overflow:hidden}
.bs-tail pre{margin:0;font-family:var(--font-mono,ui-monospace,Menlo,monospace);font-size:11.5px;line-height:1.6}
.bs-tail .ln{display:grid;grid-template-columns:54px minmax(0,1fr);padding-right:12px}
.bs-tail .ln span:first-child{text-align:right;padding-right:12px;color:var(--ui-text-quaternary);user-select:none}
.bs-tail .ln span:last-child{color:var(--ui-text-secondary);white-space:pre-wrap;word-break:break-all}
.bs-tail .ln[data-bad=true] span:last-child{color:var(--ui-red)}
.bs-tail .ln[data-bad=true]{background:color-mix(in srgb,var(--ui-red) 8%,transparent)}
.bs-caret{display:inline-block;width:7px;height:13px;vertical-align:-2px;margin-left:2px;background:var(--ui-accent);animation:bs-blink 1.1s steps(1) infinite}
@keyframes bs-blink{50%{opacity:0}}
.bs-empty{display:flex;flex-direction:column;align-items:center;gap:10px;margin:70px auto;max-width:520px;text-align:center;color:var(--ui-text-secondary)}
.bs-empty code{padding:8px 12px;border-radius:9px;background:var(--ui-bg-tertiary);font-family:var(--font-mono,ui-monospace,Menlo,monospace);font-size:12px;color:var(--ui-text-primary)}
.bs-chipbtn{display:inline-flex;align-items:center;gap:6px;padding:0 6px;height:100%;background:none;border:0;color:var(--ui-text-tertiary);font:inherit;font-size:11px;cursor:pointer}
.bs-chipbtn:hover{color:var(--ui-text-primary)}
@container (max-width:900px){.bs-body{grid-template-columns:minmax(0,1fr) 250px}.bs-ringcol{grid-column:1 / -1;flex-direction:row;justify-content:flex-start;gap:20px}.bs-ring{width:120px;height:120px}.bs-pct{font-size:27px}.bs-pct small{font-size:13px}}
@container (max-width:620px){.bs-body{grid-template-columns:minmax(0,1fr);gap:14px}.bs-ringcol{flex-wrap:wrap}.bs-tl{max-height:112px}.bs-side{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px}.bs-ticks i{height:7px}.bs-wakes strong{font-size:22px}.bs-banner{margin-top:8px;padding:6px 10px}.bs-banner code{display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical;overflow:hidden}.bs-tail{margin-top:6px}.bs-tail .ln:not(:nth-last-child(-n+5)){display:none}.bs-tail pre{font-size:11px;line-height:1.5}.bs-ring{width:116px;height:116px}.bs-pct{font-size:25px}.bs-pct small{font-size:12px}.bs-step{font-size:10px}.bs-banner code{display:none}.bs-tl{max-height:none}.bs-tl li:not(:nth-last-child(-n+4)){display:none}.bs-tail{padding:5px 0}.bs-head{padding-top:10px}.bs-tail .ln span:last-child{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.bs-tail .ln{grid-template-columns:40px minmax(0,1fr)}.bs-path{display:none}.bs-head{padding:12px 12px 0}.bs-body{padding:12px 12px 4px}.bs-banner,.bs-tail{margin-left:12px;margin-right:12px}}
@media (prefers-reduced-motion:reduce){.bs-dot,.bs-banner,.bs-ring .arc,.bs-tl li,.bs-mk,.bs-caret{animation:none!important;transition:none!important}}
`

// ---------------------------------------------------------------- pieces

function useWidth(ref) {
  const [width, setWidth] = useState(0)
  useEffect(() => {
    const node = ref.current
    if (!node) return undefined
    const observer = new ResizeObserver(entries => setWidth(Math.floor(entries[0].contentRect.width)))
    observer.observe(node)
    setWidth(Math.floor(node.getBoundingClientRect().width))
    return () => observer.disconnect()
  }, [ref])
  return width
}

function Ring({ watch, tone }) {
  const known = watch.frac !== null
  const frac = known ? Math.min(1, Math.max(0, watch.frac)) : 0
  const finish = watch.eta !== null && watch.state === 'running'
    ? new Date(Date.now() + watch.eta * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : null
  return h('div', { className: 'bs-ringcol' },
    h('div', { className: 'bs-ring', 'data-indeterminate': !known, role: 'img',
      'aria-label': known ? `${Math.round(frac * 100)} percent done` : 'progress unknown' },
      h('svg', { viewBox: '0 0 120 120' },
        h('circle', { className: 'track', cx: 60, cy: 60, r: RING_RADIUS }),
        h('circle', { className: 'arc', cx: 60, cy: 60, r: RING_RADIUS,
          strokeDasharray: known ? RING_LENGTH : undefined, strokeDashoffset: known ? RING_LENGTH * (1 - frac) : undefined })),
      h('div', { className: 'bs-ringtxt' },
        known ? h('div', { className: 'bs-pct' }, String(Math.floor(frac * 100)), h('small', null, '%')) : h('div', { className: 'bs-pct' }, '···'),
        h('div', { className: 'bs-step' }, watch.step !== null ? (watch.total ? `step ${watch.step} / ${watch.total}` : `step ${watch.step}`) : 'no step in log'))),
    h('div', null,
      h('div', { className: 'bs-eta' },
        watch.state !== 'running' ? (tone === 'alarm' ? 'stopped early' : 'finished')
          : frac >= 1 ? 'done'
          : watch.eta !== null ? h('span', null, 'ETA ', h('b', null, fmtDuration(watch.eta)), finish ? ` · ${finish}` : '')
          : 'ETA once the rate settles'),
      h('div', { className: 'bs-stats' },
        watch.rate ? h('span', { className: 'bs-pill' }, `${fmtNum(watch.rate)} it/s`) : null,
        watch.epoch && watch.epoch[0] !== null ? h('span', { className: 'bs-pill' }, watch.epoch[1] ? `epoch ${watch.epoch[0]}/${watch.epoch[1]}` : `epoch ${watch.epoch[0]}`) : null,
        h('span', { className: 'bs-pill' }, `${watch.lines} lines`))))
}

function Chart({ watch }) {
  const keys = Object.keys(watch.series).filter(key => watch.series[key].length > 0 && !CLOCKS.has(key))
  const [picked, setPicked] = useState(null)
  const [smooth, setSmooth] = useState(true)
  const [hover, setHover] = useState(null)
  const box = useRef(null)
  const width = useWidth(box)
  const key = picked && watch.series[picked] ? picked : watch.primary && watch.series[watch.primary] ? watch.primary : keys[0]
  const points = key ? watch.series[key] : []
  const height = width < 480 ? 150 : width < 760 ? 158 : 296
  const m = { l: 46, r: 14, t: 18, b: 24 }
  const chips = h('div', { className: 'bs-charthead' },
    h('div', { className: 'bs-chips' }, keys.map(name => h('button', { key: name, type: 'button', className: 'bs-chip',
      'aria-pressed': name === key, onClick: () => setPicked(name) }, SERIES_LABEL[name] || name))),
    h('button', { type: 'button', className: 'bs-switch', 'aria-pressed': smooth, onClick: () => setSmooth(v => !v) }, h('i', null), 'Smooth'))
  if (!key || points.length < 2 || width < 50) {
    return h('div', { className: 'bs-chart' }, chips, h('div', { className: 'bs-plot', ref: box, style: { height } },
      h('div', { className: 'bs-sub', style: { paddingTop: height / 2 - 10, textAlign: 'center' } },
        key ? 'Waiting for more samples' : 'No numeric metrics in the log yet')))
  }
  const raw = points.map(p => p[2])
  const shown = smooth ? ema(raw, 0.22) : raw
  const eventXs = watch.events.map(e => e.x)
  const x0 = Math.min(points[0][0], ...eventXs)
  const x1 = Math.max(points[points.length - 1][0], ...eventXs, x0 + 1)
  let lo = Math.min(...raw, ...shown)
  let hi = Math.max(...raw, ...shown)
  if (hi - lo < 1e-12) { lo -= Math.abs(lo) * 0.1 || 1; hi += Math.abs(hi) * 0.1 || 1 }
  const pad = (hi - lo) * 0.08
  lo -= pad; hi += pad
  const iw = width - m.l - m.r
  const ih = height - m.t - m.b
  const sx = x => m.l + ((x - x0) / (x1 - x0)) * iw
  const sy = v => m.t + (1 - (v - lo) / (hi - lo)) * ih
  const line = values => values.map((v, i) => `${i ? 'L' : 'M'}${sx(points[i][0]).toFixed(1)} ${sy(v).toFixed(1)}`).join(' ')
  const path = line(shown)
  const area = `${path} L${sx(points[points.length - 1][0]).toFixed(1)} ${m.t + ih} L${sx(points[0][0]).toFixed(1)} ${m.t + ih} Z`
  const yTicks = niceTicks(lo, hi, 4)
  const xTicks = niceTicks(x0, x1, width < 480 ? 3 : 6)
  const gradId = `bs-g-${watch.watch_id}`
  const zone = watch.events.find(e => e.kind === 'nan')
  const last = points[points.length - 1]
  const onMove = event => {
    const rect = event.currentTarget.getBoundingClientRect()
    const px = event.clientX - rect.left
    const x = x0 + ((px - m.l) / iw) * (x1 - x0)
    const idx = nearest(points, x)
    const near = watch.events.find(e => Math.abs(sx(e.x) - px) < 9)
    setHover({ px, idx, event: near || null })
  }
  const hp = hover ? points[hover.idx] : null
  const tipLeft = hover ? (hover.px > width - 230 ? hover.px - 220 : hover.px + 14) : 0
  return h('div', { className: 'bs-chart' }, chips,
    h('div', { className: 'bs-plot', ref: box, style: { height } },
      h('svg', { width, height, viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': `${key} over ${watch.step !== null ? 'steps' : 'lines'}` },
        h('defs', null, h('linearGradient', { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 },
          h('stop', { offset: '0%', style: { stopColor: 'var(--ui-accent)', stopOpacity: 0.34 } }),
          h('stop', { offset: '100%', style: { stopColor: 'var(--ui-accent)', stopOpacity: 0 } }))),
        yTicks.map(v => h('g', { key: `y${v}` },
          h('line', { className: 'bs-grid-line', x1: m.l, x2: m.l + iw, y1: sy(v), y2: sy(v) }),
          h('text', { className: 'bs-axis', x: m.l - 8, y: sy(v), textAnchor: 'end', dominantBaseline: 'central' }, fmtNum(v)))),
        xTicks.map(v => h('text', { key: `x${v}`, className: 'bs-axis', x: sx(v), y: height - 6, textAnchor: 'middle' }, String(Math.round(v)))),
        zone ? h('g', null,
          h('rect', { className: 'bs-zone', x: sx(zone.x), y: m.t, width: Math.max(0, m.l + iw - sx(zone.x)), height: ih }),
          sx(zone.x) < m.l + iw - 110 ? h('text', { className: 'bs-zonelabel', x: sx(zone.x) + 10, y: m.t + 16 }, 'NON-FINITE') : null) : null,
        h('path', { className: 'bs-area', d: area, fill: `url(#${gradId})` }),
        smooth ? h('path', { className: 'bs-raw', d: line(raw) }) : null,
        h('path', { className: 'bs-line', d: path }),
        zone && last[0] <= zone.x + 1 ? h('circle', { className: 'bs-endcap', cx: sx(last[0]), cy: sy(shown[shown.length - 1]), r: 5 }) : null,
        watch.events.map((e, i) => h('g', { key: `e${i}`, className: 'bs-mk', 'data-kind': e.kind, 'data-new': watch.uptime - e.t < 6 },
          h('line', { x1: sx(e.x), x2: sx(e.x), y1: m.t + 8, y2: m.t + ih }),
          h('circle', { cx: sx(e.x), cy: m.t + 2, r: 8 }),
          h('text', { x: sx(e.x), y: m.t + 2 }, KIND_GLYPH[e.kind] || '■'))),
        hover ? h('g', null,
          h('line', { className: 'bs-cursor', x1: sx(hp[0]), x2: sx(hp[0]), y1: m.t, y2: m.t + ih }),
          h('circle', { className: 'bs-hoverdot', cx: sx(hp[0]), cy: sy(shown[hover.idx]), r: 4.5 })) : null,
        h('rect', { x: m.l, y: 0, width: iw, height, fill: 'transparent', onPointerMove: onMove, onPointerLeave: () => setHover(null) })),
      hover ? h('div', { className: 'bs-tip', style: { left: tipLeft, top: 14 } },
        hover.event
          ? [h('b', { key: 'b' }, KIND_LABEL[hover.event.kind] || hover.event.kind, hover.event.count > 1 ? ` ×${hover.event.count}` : ''),
            h('div', { key: 'r', className: 'row' }, h('span', null, watch.step !== null ? 'step' : 'line'), h('span', null, String(hover.event.x))),
            h('div', { key: 'r2', className: 'row' }, h('span', null, 'at'), h('span', null, clock(hover.event.t))),
            hover.event.text ? h('code', { key: 'c' }, hover.event.text) : null]
          : [h('b', { key: 'b' }, `${watch.step !== null ? 'step' : 'line'} ${hp[0]}`),
            h('div', { key: 'r', className: 'row' }, h('span', null, key), h('span', null, fmtNum(raw[hover.idx]))),
            smooth ? h('div', { key: 'r2', className: 'row' }, h('span', null, 'smoothed'), h('span', null, fmtNum(shown[hover.idx]))) : null,
            h('div', { key: 'r3', className: 'row' }, h('span', null, 'at'), h('span', null, clock(hp[1])))]) : null))
}

/** Share of its limit the process holds; under 1% says so instead of rounding to a flat zero. */
const memPct = mem => (mem.rss / mem.limit < 0.01 ? '<1%' : `${Math.round((mem.rss / mem.limit) * 100)}%`)

function Wakes({ watch }) {
  const budget = watch.wake_budget || 20
  const quiet = watch.stall_seconds ? fmtDuration(watch.stall_seconds) : null
  return h('div', null,
    h('div', { className: 'bs-label' }, h('span', null, 'Agent wakes'), h('span', null, `budget ${budget}`)),
    h('div', { className: 'bs-wakes' }, h('strong', null, String(watch.repeat_wakes)), h('span', { className: 'bs-sub' },
      `of ${budget}` + (watch.suppressed ? ` · ${watch.suppressed} folded` : ''))),
    h('div', { className: 'bs-ticks', 'aria-hidden': true }, Array.from({ length: budget }, (_, i) =>
      h('i', { key: i, 'data-on': i < watch.repeat_wakes, 'data-last': i === watch.repeat_wakes - 1 && watch.repeat_wakes >= budget }))),
    h('div', { className: 'bs-facts' },
      h('span', null, h('b', null, String(watch.model_calls)), ' model calls while watching'),
      quiet ? h('span', null, 'stall after ', h('b', null, quiet), ' quiet') : null,
      watch.mem ? h('span', null, 'memory ', h('b', null, fmtBytes(watch.mem.rss)), ` · ${memPct(watch.mem)} of ${fmtBytes(watch.mem.limit)}`) : null),
    watch.mem ? h('div', { className: 'bs-mem', 'data-hot': watch.mem.rss / watch.mem.limit >= 0.9, 'aria-hidden': true },
      h('i', { style: { width: `${Math.min(100, Math.max(1, (watch.mem.rss / watch.mem.limit) * 100))}%` } })) : null)
}

function Timeline({ watch }) {
  const rows = watch.events.slice(-8)
  return h('div', null,
    h('div', { className: 'bs-label' }, h('span', null, 'Events'), h('span', null, `${watch.events.length}`)),
    rows.length === 0
      ? h('div', { className: 'bs-sub' }, 'Nothing has gone wrong yet.')
      : h('ul', { className: 'bs-tl' }, rows.map((e, i) => h('li', { key: `${e.kind}${e.t}${i}`, 'data-kind': e.kind },
        h('time', null, clock(e.t)), h('span', { className: 'k' }),
        h('span', { className: 't', title: e.text },
          h('b', null, KIND_LABEL[e.kind] || e.kind), e.count > 1 ? `×${e.count} ` : '', e.text)))))
}

const BAD_LINE = /\bnan\b|\binf(?:inity)?\b|traceback|error|exception|out of memory|killed/i

function Tail({ watch }) {
  const first = watch.lines - watch.tail.length
  return h('div', { className: 'bs-tail' }, h('pre', null, watch.tail.length === 0
    ? h('div', { className: 'ln' }, h('span', null, ''), h('span', null, 'waiting for log output'))
    : watch.tail.map((text, i) => h('div', { key: i, className: 'ln', 'data-bad': BAD_LINE.test(text) },
      h('span', null, String(first + i + 1)), h('span', null, text, i === watch.tail.length - 1 && watch.state === 'running' ? h('i', { className: 'bs-caret' }) : null)))))
}

function WatchCard({ watch }) {
  const tone = toneOf(watch)
  const lead = headline(watch)
  const age = watch.state === 'lost' ? ` · silent ${fmtDuration(watch.age)}` : ''
  return h('article', { className: 'bs-card', 'data-tone': tone },
    h('div', { className: 'bs-head' },
      h('span', { className: 'bs-dot', 'data-pulse': watch.state === 'running' }),
      h('span', { className: 'bs-name' }, watch.name),
      h('span', { className: 'bs-path', title: watch.path }, watch.pid ? `${watch.path}  ·  pid ${watch.pid}` : watch.path),
      h('span', { className: 'bs-state' },
        watch.state === 'running' ? `watching ${fmtDuration(watch.uptime)}` : watch.state === 'lost' ? 'watcher lost' + age : watch.state)),
    lead && (tone === 'alarm' || tone === 'warn')
      ? h('div', { className: 'bs-banner', role: 'status' }, h('div', null,
        h('b', null, KIND_LABEL[lead.kind] || lead.kind), lead.count > 1 ? ` ×${lead.count}` : '',
        ` at ${watch.step !== null ? 'step' : 'line'} ${lead.x}, ${clock(lead.t)} in. `,
        lead.text ? h('code', null, lead.text) : null,
        watch.detail && watch.state !== 'running' ? h('div', null, h('b', null, 'Ended: '), watch.detail) : null))
      : null,
    h('div', { className: 'bs-body' },
      h(Ring, { watch, tone }), h(Chart, { watch }),
      h('div', { className: 'bs-side' }, h(Wakes, { watch }), h(Timeline, { watch }))),
    watch.wake_delivery === 'chat' ? h('div', { className: 'bs-banner', style: { '--bs-tone': 'var(--ui-yellow)' } }, h('div', null, h('b', null, 'Wakes post to the chat. '), watch.wake_note || '')) : null,
    watch.notify_error ? h('div', { className: 'bs-banner', style: { '--bs-tone': 'var(--ui-yellow)' } }, h('div', null, h('b', null, 'Delivery: '), watch.notify_error)) : null,
    h(Tail, { watch }))
}

function Empty({ error }) {
  return h('div', { className: 'bs-empty' },
    h('svg', { width: 56, height: 56, viewBox: '0 0 56 56', fill: 'none', 'aria-hidden': true },
      h('circle', { cx: 28, cy: 28, r: 22, stroke: 'var(--ui-stroke-secondary)', strokeWidth: 4 }),
      h('path', { d: 'M28 6a22 22 0 0 1 22 22', stroke: 'var(--ui-accent)', strokeWidth: 4, strokeLinecap: 'round' })),
    error
      ? h('div', null, 'Babysit cannot reach its route. Add babysit to plugins.enabled in config.yaml, restart the gateway, then reload the plugin.')
      : h('div', null, 'No watches yet. Start a job and ask Hermes to watch its log:'),
    error ? h('code', null, String(error.message || error)) : h('code', null, 'Babysit /tmp/train.log, pid 4021, wake me if anything breaks.'))
}

function useWatches() {
  return useQuery({
    queryKey: ['plugin', ID, 'watches'],
    queryFn: () => rest('/watches'),
    refetchInterval: POLL_MS,
    retry: 1
  })
}

function View({ pane }) {
  const { data, error } = useWatches()
  const watches = data ? data.watches : []
  const live = watches.filter(w => w.state === 'running').length
  return h('div', { className: 'bs-root', 'data-pane': pane },
    h('div', { className: 'bs-top' },
      h('span', { className: 'bs-title' }, 'Babysit'),
      h('span', { className: 'bs-pill' }, h('span', { className: 'bs-dot', 'data-pulse': live > 0, style: { '--bs-tone': live ? 'var(--ui-green)' : 'var(--ui-text-quaternary)' } }),
        live ? `${live} watching` : 'idle'),
      watches.length > live ? h('span', { className: 'bs-sub' }, `${watches.length - live} ended`) : null),
    watches.length === 0
      ? h(Empty, { error })
      : h('div', { className: 'bs-grid' }, watches.map(watch => h(WatchCard, { key: watch.watch_id, watch }))))
}

function Chip() {
  const { data } = useWatches()
  const watches = data ? data.watches : []
  const live = watches.filter(w => w.state === 'running')
  if (live.length === 0) return null
  const alarm = live.some(w => toneOf(w) === 'alarm')
  return h('button', { type: 'button', className: 'bs-chipbtn', onClick: () => host.navigate('/babysit'), title: 'Open Babysit' },
    h('span', { className: 'bs-dot', 'data-pulse': true, style: { '--bs-tone': alarm ? 'var(--ui-red)' : 'var(--ui-green)' } }),
    alarm ? 'job needs you' : `${live.length} watching`)
}

export default {
  id: ID,
  name: 'Babysit',
  register(ctx) {
    rest = (path, opts) => ctx.rest(path, opts)
    const style = document.createElement('style')
    style.textContent = CSS
    document.head.append(style)
    ctx.onDispose(() => {
      style.remove()
      rest = null
    })
    ctx.registerMany([
      { id: 'pane', area: PANES_AREA, title: 'Babysit', data: { placement: 'right', width: '420px' }, render: () => h(View, { pane: true }) },
      { id: 'page', area: ROUTES_AREA, data: { path: '/babysit' }, render: () => h(View, { pane: false }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/babysit', label: 'Babysit', codicon: 'pulse' } },
      { id: 'chip', area: STATUSBAR_AREAS.right, order: 110, render: () => h(Chip, {}) },
      { id: 'open', area: PALETTE_AREA, data: { id: 'babysit.open', label: 'Open Babysit', keywords: ['babysit', 'watch', 'training', 'log', 'job'], run: () => host.navigate('/babysit') } }
    ])
  }
}
