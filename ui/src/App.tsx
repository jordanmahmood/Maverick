import { useEffect, useState } from "react"
import { ChevronLeft, ChevronRight, RotateCcw, Volume2 } from "lucide-react"

import { LiveView } from "@/components/live-view"
import { Settings } from "@/components/settings"
import { Steps } from "@/components/steps"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { command, connect, send, useHub, voiceOn } from "@/lib/hub"

function savedPhoneView(): boolean {
  try { return localStorage.getItem("view") !== "glasses" } catch { return true }
}

/** The pink rectangle the wearer lines their lens up with. The hub looks for this colour. */
function LensAlignment() {
  const hub = useHub()
  if (!hub.calibrating) return null
  return (
    <div className="fixed inset-0 z-[60] grid place-content-center justify-items-center gap-6 bg-black text-center">
      <div className="aspect-[4/3] h-[58vh] bg-[#ff2bd6]" />
      <p className="max-w-xl text-lg text-white/80">
        Look at this screen through the glasses. Move until the green frame on the lens fits the
        pink rectangle, edge to edge. Then tap the touchpad.
      </p>
      <div className="flex gap-2">
        <Button variant="brand" onClick={() => send({ type: "calib", action: "capture" })}>Capture now</Button>
        <Button variant="outline" onClick={() => send({ type: "calib", action: "stop" })}>Cancel</Button>
      </div>
      {hub.calibError && <p className="text-warn" role="alert">{hub.calibError}</p>}
    </div>
  )
}

export default function App() {
  const hub = useHub()
  const [phoneView, setPhoneView] = useState(savedPhoneView)

  useEffect(connect, [])
  useEffect(() => {
    try { localStorage.setItem("view", phoneView ? "phone" : "glasses") } catch { /* private window */ }
  }, [phoneView])
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement
      if (target.closest("input, textarea, [role=dialog]")) return
      if (event.key === "ArrowRight") command("next")
      if (event.key === "ArrowLeft") command("back")
      if (event.key.toLowerCase() === "d") send({ type: "debug", verdict: "done" })
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [])

  const total = hub.steps.length
  const status = !hub.connected ? "Reconnecting" : hub.cameras ? "Live" : "No camera"
  const live = hub.connected && hub.cameras > 0

  return (
    <div className="mx-auto flex min-h-screen max-w-[1500px] flex-col px-6 sm:px-10">
      <header className="flex h-16 items-center justify-between">
        <span className="text-[17px] font-semibold tracking-tight">Maverick</span>
        <div className="flex items-center gap-4">
          <span className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
            <span className={cn("size-2 rounded-full", live ? "animate-pulse-dot bg-brand" : "bg-white/25")} />
            {status}
          </span>
          {hub.soundBlocked && voiceOn() && (
            <Button variant="outline" size="sm">
              <Volume2 /> Turn on sound
            </Button>
          )}
          <Settings phoneView={phoneView} onPhoneView={setPhoneView} />
        </div>
      </header>

      <main className="grid flex-1 items-start gap-10 pt-2 pb-12 lg:grid-cols-[minmax(0,1fr)_380px] lg:gap-14">
        <LiveView phoneView={phoneView} />

        <aside className="lg:pt-2">
          <h1 className="text-[32px] leading-[1.1] font-medium">{hub.title || "Maverick"}</h1>
          <p className="num mt-2 text-muted-foreground">
            {hub.stage === "setup"
              ? "Choosing the dominant eye on the glasses"
              : hub.finished ? "All done" : total ? `Step ${hub.index + 1} of ${total}` : " "}
          </p>

          <div className="mt-8">
            <Steps />
          </div>

          <p className="mt-6 min-h-[1.5rem] text-sm text-muted-foreground" aria-live="polite">
            {hub.note || hub.seen}
          </p>

          <div className="mt-6 flex items-center gap-2">
            <Button variant="outline" onClick={() => command("back")} disabled={(hub.index === 0 && !hub.finished) || hub.stage === "setup"}>
              <ChevronLeft /> Back
            </Button>
            <Button variant="default" onClick={() => command("next")} disabled={hub.finished || hub.stage === "setup"}>
              Next <ChevronRight />
            </Button>
            <span className="flex-1" />
            <Button variant="outline" onClick={() => send({ type: "setup" })}>
              <RotateCcw /> Restart demo
            </Button>
          </div>
        </aside>
      </main>

      <LensAlignment />
    </div>
  )
}
