import { Check } from "lucide-react"

import { cn } from "@/lib/utils"
import { send, useHub } from "@/lib/hub"

/** The build, one row per step. Click a row to jump there. */
export function Steps() {
  const hub = useHub()
  return (
    <ol className="-mx-2">
      {hub.steps.map((step, i) => {
        const done = hub.finished || i < hub.index
        const current = !hub.finished && i === hub.index
        return (
          <li key={step.id}>
            <button
              type="button"
              onClick={() => send({ type: "goto", index: i })}
              aria-current={current ? "step" : undefined}
              className={cn(
                "flex w-full items-center gap-3 rounded-2xl px-2 py-2.5 text-left transition-colors outline-none",
                "hover:bg-white/[0.04] focus-visible:ring-2 focus-visible:ring-ring",
                current && "bg-white/[0.05]",
              )}
            >
              <span
                className={cn(
                  "grid size-12 shrink-0 place-items-center overflow-hidden rounded-xl bg-white/[0.04]",
                  !current && !done && "opacity-50",
                )}
              >
                {step.cls ? (
                  <img
                    src={`/guides/kits/demo/parts/${step.cls}.png`}
                    alt=""
                    className="size-10 object-contain"
                    onError={(event) => { event.currentTarget.style.visibility = "hidden" }}
                  />
                ) : (
                  <span className="num text-sm text-muted-foreground">{i + 1}</span>
                )}
              </span>
              <span
                className={cn(
                  "min-w-0 flex-1 text-[15px] leading-snug",
                  current ? "font-medium text-foreground" : "text-muted-foreground",
                )}
              >
                {step.say}
              </span>
              <span className="grid size-5 shrink-0 place-items-center">
                {done && <Check className="size-4 text-brand" aria-label="Done" />}
                {current && <span className="size-2 animate-pulse-dot rounded-full bg-brand" />}
              </span>
            </button>
          </li>
        )
      })}
    </ol>
  )
}
