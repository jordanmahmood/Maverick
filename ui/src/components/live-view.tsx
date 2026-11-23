import { useEffect, useRef, useState } from "react"

import { cn } from "@/lib/utils"
import { live, useHub, type Away, type Box, type Target } from "@/lib/hub"

const ACCENT = "#39ff14"
const WRONG = "#ff5c5c"

/**
 * What the wearer sees: the live camera with the same instruction bar, boxes and labels
 * the lens draws. All HUD sizes are in container-width units, so it matches the 640-wide
 * lens at any size on this page.
 */
export function LiveView({ phoneView }: { phoneView: boolean }) {
  const hub = useHub()
  const frame = useRef<HTMLImageElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  const view = useRef<Box>([0, 0, 1, 1])
  const [now, setNow] = useState(() => performance.now())

  // On the glasses the display covers only part of the camera's view; on a phone, all of it.
  const area: Box = phoneView ? [0, 0, 1, 1] : hub.calib
  view.current = area
  const w = area[2] - area[0]
  const h = area[3] - area[1]

  useEffect(() => {
    let raf = 0
    let seq = -1
    const drawn = new Map<string, Box>()  // eased boxes, so they glide between detections
    const paint = (time: number) => {
      raf = requestAnimationFrame(paint)
      if (frame.current && live.frame && seq !== live.frameSeq) {
        seq = live.frameSeq
        frame.current.src = live.frame
      }
      const el = canvas.current
      if (!el) return
      const ratio = window.devicePixelRatio || 1
      const W = Math.round(el.clientWidth * ratio)
      const H = Math.round(el.clientHeight * ratio)
      if (el.width !== W || el.height !== H) {
        el.width = W
        el.height = H
      }
      const g = el.getContext("2d")!
      g.clearRect(0, 0, W, H)
      const age = time - live.targetsAt
      const fade = age < 1200 ? 1 : Math.max(0, 1 - (age - 1200) / 400)
      if (fade <= 0) {
        drawn.clear()
        return
      }
      const v = view.current
      const ids = new Set(live.targets.map((t) => t.id))
      for (const id of drawn.keys()) if (!ids.has(id)) drawn.delete(id)
      for (const target of live.targets) {
        if (target.box?.length !== 4) continue
        const goal = target.box.map((value, i) =>
          i % 2 === 0 ? (value - v[0]) / (v[2] - v[0]) : (value - v[1]) / (v[3] - v[1])) as Box
        const from = drawn.get(target.id) ?? goal
        const eased = from.map((value, i) => value + (goal[i] - value) * 0.3) as Box
        drawn.set(target.id, eased)
        drawTarget(g, W, H, target, eased, fade, time)
      }
      for (const pointer of live.away) drawAway(g, W, H, pointer, fade, time)
    }
    raf = requestAnimationFrame(paint)
    return () => cancelAnimationFrame(raf)
  }, [])

  // Re-render when the peek image or the "done" tick should disappear.
  useEffect(() => {
    const until = Math.max(hub.peekUntil, hub.flashAt + 950)
    const wait = until - performance.now()
    setNow(performance.now())
    if (wait <= 0) return
    const timer = setTimeout(() => setNow(performance.now()), wait + 20)
    return () => clearTimeout(timer)
  }, [hub.peekUntil, hub.flashAt])

  const { screen, hint } = hub
  const info = screen.mode === "info"
  const choice = screen.mode === "choice"
  const total = screen.total ?? hub.steps.length
  const current = screen.step ?? hub.index + 1
  const line = hint.text || screen.sub
  const showDone = hub.flashAt > 0 && now - hub.flashAt < 950
  const showPeek = !!screen.image && now < hub.peekUntil

  return (
    <div className="relative aspect-[4/3] w-full overflow-hidden rounded-3xl bg-black ring-1 ring-white/[0.07] [container-type:inline-size]">
      <img
        ref={frame}
        alt=""
        className={cn("absolute max-w-none select-none", !hub.hasFrame && "hidden")}
        style={{
          width: `${100 / w}%`, height: `${100 / h}%`,
          left: `${(-area[0] / w) * 100}%`, top: `${(-area[1] / h) * 100}%`,
        }}
      />
      <canvas ref={canvas} className="pointer-events-none absolute inset-0 size-full" />

      {!hub.hasFrame && !choice && (
        <div className="absolute inset-0 grid place-items-center px-8 text-center">
          <div>
            <p className="text-lg font-medium">Waiting for the camera</p>
            <p className="mt-1 text-sm text-muted-foreground">
              Connect the glasses or a phone, then run ./device.sh
            </p>
          </div>
        </div>
      )}

      {showPeek && (
        <img
          src={screen.image}
          alt=""
          className="absolute right-[2.4cqw] bottom-[4cqw] max-h-[40%] max-w-[44%] animate-rise rounded-[2.2cqw] bg-black/80"
        />
      )}

      {showDone && (
        <div className="pointer-events-none absolute inset-0 grid place-items-center">
          <svg viewBox="0 0 72 72" className="size-[11.25cqw]" fill="none" stroke={hub.flashKind === "wrong" ? WRONG : ACCENT} strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="36" cy="36" r="33" strokeDasharray="208" strokeDashoffset="208" className="animate-ring" transform="rotate(-90 36 36)" />
            {hub.flashKind === "wrong" ? (
              <path d="M25 25 47 47 M47 25 25 47" strokeDasharray="64" strokeDashoffset="64" className="animate-tick" />
            ) : (
              <path d="M23 37.5 32.5 47 50 27.5" strokeDasharray="42" strokeDashoffset="42" className="animate-tick" />
            )}
          </svg>
        </div>
      )}

      {hub.hasFrame && info && screen.big && (
        <div className="absolute inset-x-[8%] bottom-[14%] text-center">
          <p className="text-[4cqw] leading-tight font-semibold text-white">{screen.big}</p>
          {screen.sub && <p className="mt-[1cqw] text-[2.34cqw] text-white/70">{screen.sub}</p>}
        </div>
      )}

      {choice && (
        <div className="absolute inset-0 grid place-content-center justify-items-center gap-[2.6cqw] bg-black/55 pb-[6cqw] text-center">
          <p className="text-[3.44cqw] font-semibold text-white">{screen.big}</p>
          <div className="flex gap-[2.5cqw]">
            {(screen.options ?? []).map((option, i) => (
              <span
                key={option}
                className={cn(
                  "grid h-[9.4cqw] w-[29.7cqw] place-items-center rounded-full text-[2.8cqw] font-semibold",
                  i === screen.selected
                    ? "bg-brand text-brand-foreground"
                    : "text-white/70 ring-[0.23cqw] ring-white/30",
                )}
              >
                {option}
              </span>
            ))}
          </div>
          {screen.sub && <p className="text-[2.19cqw] text-white/70">{screen.sub}</p>}
        </div>
      )}

      {/* The instruction bar, with the lens's listening pill just clear of it. */}
      <div className="pointer-events-none absolute inset-x-0 top-[2.2cqw] flex flex-col items-center gap-[1.25cqw]">
      {hub.hasFrame && !info && !choice && screen.big && (
        <div
          className={cn(
            "relative flex max-w-[88%] min-w-[46%] items-center gap-[1.9cqw] rounded-[2.8cqw] p-[2.2cqw]",
            phoneView && "bg-black/55 backdrop-blur-md",
          )}
        >
          {hub.micWaiting && (
            <span className="absolute top-1/2 right-[1.1cqw] size-[0.94cqw] -translate-y-1/2 rounded-full bg-brand opacity-50" aria-label="Maverick heard something" />
          )}
          {screen.icon && (
            <img src={screen.icon} alt="" className="size-[8.1cqw] shrink-0 object-contain" />
          )}
          <div className="min-w-0">
            {total > 1 && (
              <div className="mb-[1.1cqw] flex items-center gap-[0.95cqw]" aria-label={`Step ${current} of ${total}`}>
                {Array.from({ length: total }, (_, i) => (
                  <span
                    key={i}
                    className={cn(
                      "rounded-full",
                      i + 1 < current && "size-[0.95cqw] bg-brand",
                      i + 1 === current && "size-[1.25cqw] bg-white",
                      i + 1 > current && "size-[0.95cqw] bg-white/30",
                    )}
                  />
                ))}
              </div>
            )}
            <p className={cn("text-[3.125cqw] leading-[1.15] font-semibold", screen.warn ? "text-warn" : "text-white")}>
              {screen.big}
            </p>
            {line && (
              <p
                className={cn(
                  "mt-[0.6cqw] truncate text-[2.34cqw]",
                  !hint.text && "text-white/70",
                  hint.text && hint.tone === "info" && "text-white/70",
                  hint.text && hint.tone === "ok" && "text-brand",
                  hint.text && hint.tone === "warn" && "text-warn",
                )}
              >
                {line}
              </p>
            )}
          </div>
        </div>
      )}
      {hub.ptt !== "off" && !choice && (
        <div
          role="status"
          className={cn(
            "flex items-center gap-[0.94cqw] rounded-full bg-black py-[0.78cqw] pr-[1.875cqw] pl-[1.56cqw] text-[2.19cqw] leading-[1.3] font-semibold ring-[0.16cqw]",
            hub.ptt === "listening" ? "animate-listen text-brand ring-brand/60" : "text-white/70 ring-white/25",
          )}
        >
          <svg viewBox="0 0 12 16" className="h-[2.5cqw] w-[1.875cqw]" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden>
            <rect x="3.6" y="1.6" width="4.8" height="7.7" rx="2.4" />
            <path d="M10.4 6.9A4.4 5.3 0 0 1 1.6 6.9M6 12.2v2.2" />
          </svg>
          {hub.ptt === "listening" ? "Listening…" : "Thinking…"}
        </div>
      )}
      </div>
    </div>
  )
}

/** "It's over there": an arrow at the edge of the view, with the part's name beside it. */
function drawAway(g: CanvasRenderingContext2D, W: number, H: number, pointer: Away, fade: number, time: number) {
  if (Math.abs(pointer.dx) <= 0.5 && Math.abs(pointer.dy) <= 0.5) return
  const u = W / 640
  const angle = Math.atan2(pointer.dy * H, pointer.dx * W)
  // Where the line from the centre toward the part leaves the view (kept above the bar).
  const reach = Math.min(
    (W / 2 - 26 * u) / Math.max(1e-6, Math.abs(Math.cos(angle))),
    (H * 0.36) / Math.max(1e-6, Math.abs(Math.sin(angle))),
  )
  const nudge = 4 * u * Math.sin((time / 1000) * Math.PI * 2)
  const px = W / 2 + Math.cos(angle) * (reach + nudge)
  const py = H * 0.44 + Math.sin(angle) * (reach + nudge)
  g.save()
  g.globalAlpha = fade
  g.translate(px, py)
  g.rotate(angle)
  g.strokeStyle = ACCENT
  g.lineWidth = 4 * u
  g.lineCap = "round"
  g.lineJoin = "round"
  g.beginPath()
  g.moveTo(-9 * u, -11 * u)
  g.lineTo(4 * u, 0)
  g.lineTo(-9 * u, 11 * u)
  g.stroke()
  g.restore()

  g.save()
  g.globalAlpha = fade
  g.font = `600 ${14 * u}px "Instrument Sans Variable", ui-sans-serif, system-ui, sans-serif`
  g.textBaseline = "middle"
  const pillW = g.measureText(pointer.label).width + 16 * u
  const pillH = 22 * u
  const lx = Math.min(Math.max(6 * u, px - Math.cos(angle) * (22 * u + pillW / 2) - pillW / 2), W - pillW - 6 * u)
  const ly = Math.min(Math.max(6 * u, py - Math.sin(angle) * 30 * u - pillH / 2), H * 0.74)
  roundedRect(g, lx, ly, pillW, pillH, pillH / 2)
  g.fillStyle = ACCENT
  g.fill()
  g.fillStyle = "#050505"
  g.fillText(pointer.label, lx + 8 * u, ly + pillH / 2 + 0.5 * u)
  g.restore()
}

function roundedRect(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  const radius = Math.max(0, Math.min(r, w / 2, h / 2))
  g.beginPath()
  g.roundRect(x, y, w, h, radius)
}

/** One AR highlight: a thin rounded box with a soft glow, and a pill label above it. */
function drawTarget(g: CanvasRenderingContext2D, W: number, H: number, target: Target, box: Box, fade: number, time: number) {
  const u = W / 640  // one lens pixel
  const x = box[0] * W
  const y = box[1] * H
  const w = (box[2] - box[0]) * W
  const h = (box[3] - box[1]) * H
  const place = target.style === "place"
  const colour = target.style === "remove" ? WRONG : ACCENT
  const pulse = place ? 0.825 + 0.175 * Math.sin((time / 1200) * Math.PI * 2) : 1
  const label = target.sub ? `${target.label} · ${target.sub}` : target.label
  g.save()
  g.globalAlpha = fade * pulse
  g.font = `600 ${14 * u}px "Instrument Sans Variable", ui-sans-serif, system-ui, sans-serif`
  g.textBaseline = "middle"
  const pillW = g.measureText(label).width + 16 * u
  const pillH = 22 * u

  const cx = x + w / 2
  const cy = y + h / 2
  if (cx < 0 || cx > W || cy < 0 || cy > H) {  // out of view: point toward it
    const px = Math.min(W - 18 * u, Math.max(18 * u, cx))
    const py = Math.min(H * 0.78, Math.max(18 * u, cy))
    g.translate(px, py)
    g.rotate(Math.atan2(cy - H / 2, cx - W / 2))
    g.fillStyle = ACCENT
    g.beginPath()
    g.moveTo(11 * u, 0)
    g.lineTo(-7 * u, -9 * u)
    g.lineTo(-7 * u, 9 * u)
    g.closePath()
    g.fill()
    g.restore()
    return
  }

  g.strokeStyle = colour
  if (place) g.setLineDash([10 * u, 7 * u])
  g.globalAlpha = fade * pulse * 0.22  // glow
  g.lineWidth = 7 * u
  roundedRect(g, x, y, w, h, 10 * u)
  g.stroke()
  g.globalAlpha = fade * pulse
  g.lineWidth = 2.5 * u
  roundedRect(g, x, y, w, h, 10 * u)
  g.stroke()
  g.setLineDash([])

  if (label) {
    const lx = Math.min(Math.max(4 * u, x), W - pillW - 4 * u)
    const above = y - pillH - 6 * u
    const ly = above < 4 * u ? y + h + 6 * u : above
    roundedRect(g, lx, ly, pillW, pillH, pillH / 2)
    if (place) {
      g.fillStyle = "rgba(0,0,0,0.55)"
      g.fill()
      g.lineWidth = 1.5 * u
      g.stroke()
      g.fillStyle = colour
    } else {
      g.fillStyle = colour
      g.fill()
      g.fillStyle = "#050505"
    }
    g.fillText(label, lx + 8 * u, ly + pillH / 2 + 0.5 * u)
  }
  g.restore()
}
