/**
 * The Maverick phone app (served at /app, shown inside the Android shell in phone-app/).
 *
 * The phone is where you set Maverick up for yourself and give consent; the glasses are
 * where you build. Setup hands the build over to the glasses, and while you build the phone
 * follows along: the step you are on, what Maverick noticed, and the switches that are
 * yours to flip at any time (camera, microphone, brightness, how much it says).
 */
import { useEffect, useState, type ReactNode } from "react"
import {
  BatteryCharging, BatteryMedium, BellOff, Camera, Check, ChevronRight, Cpu, Eye, Glasses, Mic, Moon, RotateCcw,
  Sun, Timer, TriangleAlert, Volume2, Wifi,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { cn } from "@/lib/utils"
import { connect, send, setEye, setPrefs, useHub, type HubState } from "@/lib/hub"

const tap = () => { try { (window as any).MaverickApp?.haptic() } catch { /* in a browser */ } }

function Row({ icon, title, hint, children }: { icon?: ReactNode; title: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex items-center gap-3.5 py-3.5">
      {icon && <span className="grid size-9 shrink-0 place-items-center rounded-full bg-white/[0.06] text-foreground [&_svg]:size-[18px]">{icon}</span>}
      <div className="min-w-0 flex-1">
        <p className="text-[15px] font-medium">{title}</p>
        {hint && <p className="mt-0.5 text-[13px] leading-snug text-muted-foreground">{hint}</p>}
      </div>
      {children}
    </div>
  )
}

function Pick<T extends string>({ value, options, onChange }: { value: T; options: [T, string][]; onChange: (v: T) => void }) {
  return (
    <div className="flex rounded-full bg-white/[0.06] p-1">
      {options.map(([id, label]) => (
        <button
          key={id}
          onClick={() => { tap(); onChange(id) }}
          className={cn(
            "h-9 rounded-full px-4 text-sm font-medium transition-colors",
            value === id ? "bg-primary text-primary-foreground" : "text-muted-foreground",
          )}
        >
          {label}
        </button>
      ))}
    </div>
  )
}

function Slider({ icon, title, value, min = 0, onChange }: { icon: ReactNode; title: string; value: number; min?: number; onChange: (v: number) => void }) {
  return (
    <div className="py-3.5">
      <div className="flex items-center gap-3.5">
        <span className="grid size-9 shrink-0 place-items-center rounded-full bg-white/[0.06] [&_svg]:size-[18px]">{icon}</span>
        <p className="flex-1 text-[15px] font-medium">{title}</p>
        <span className="num text-sm text-muted-foreground">{value}%</span>
      </div>
      <input
        type="range" min={min} max={100} step={5} value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        aria-label={title}
        className="mt-3 h-8 w-full accent-[#39ff14]"
      />
    </div>
  )
}

function Brightness({ hub }: { hub: HubState }) {
  return (
    <Slider
      icon={<Sun />} title="Lens brightness" min={10}
      value={Math.round(hub.prefs.brightness * 100)}
      onChange={(v) => setPrefs({ brightness: v / 100 })}
    />
  )
}

/** Cut the cable: put the glasses on Wi-Fi from here, and they carry on without USB. */
function WifiControls({ hub }: { hub: HubState }) {
  const wifi = hub.device.wifi ?? {}
  const [open, setOpen] = useState(false)
  const [ssid, setSsid] = useState("")
  const [password, setPassword] = useState("")
  const connected = hub.cameras > 0
  // The laptop and the glasses must be on the same network for the cable to come out.
  const sameNetwork = !!wifi.ip && hub.addresses.some((a) => a.split(".").slice(0, 2).join(".") === wifi.ip!.split(".").slice(0, 2).join("."))
  const status = !connected ? "Glasses not connected"
    : hub.link === "wifi" ? `Running over Wi-Fi${wifi.ssid ? ` (${wifi.ssid})` : ""}. No cable needed.`
    : !wifi.on ? "Wi-Fi is off. The glasses need the cable."
    : !wifi.connected ? (wifi.state ? `Wi-Fi: ${wifi.state}` : "Wi-Fi on, not joined to a network")
    : sameNetwork ? `Ready on ${wifi.ssid || "Wi-Fi"}. Unplug the cable and it carries on.`
    : `On ${wifi.ssid || "Wi-Fi"}, but the laptop is on another network.`
  const join = () => {
    if (!ssid.trim()) return
    tap()
    send({ type: "wifi", join: { ssid: ssid.trim(), password } })
    setPassword("")
    setOpen(false)
  }
  return (
    <>
      <Row icon={<Wifi />} title="Wi-Fi" hint={status}>
        <Switch
          checked={!!wifi.on} disabled={!connected}
          onCheckedChange={(enable) => { tap(); send({ type: "wifi", enable }) }}
        />
      </Row>
      <div className="py-3.5">
        {open ? (
          <div className="grid gap-2.5">
            <input
              value={ssid} onChange={(event) => setSsid(event.target.value)} placeholder="Network name"
              autoCapitalize="none" autoCorrect="off" aria-label="Network name"
              className="h-11 rounded-2xl bg-white/[0.06] px-4 text-[15px] outline-none placeholder:text-muted-foreground"
            />
            <input
              value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Password"
              type="password" aria-label="Password"
              className="h-11 rounded-2xl bg-white/[0.06] px-4 text-[15px] outline-none placeholder:text-muted-foreground"
            />
            <div className="flex gap-2">
              <Button variant="brand" className="flex-1" disabled={!ssid.trim() || !connected} onClick={join}>Join</Button>
              <Button variant="ghost" onClick={() => { setOpen(false); setPassword("") }}>Cancel</Button>
            </div>
            <p className="text-[13px] leading-snug text-muted-foreground">
              Use the same network as the laptop. A phone hotspot works well in a crowded room. The password goes
              only to the glasses.
            </p>
          </div>
        ) : (
          <Button variant="outline" size="sm" disabled={!connected} onClick={() => { tap(); setSsid(wifi.ssid ?? ""); setOpen(true) }}>
            Join a network
          </Button>
        )}
      </div>
    </>
  )
}

/** The controls the glasses' own app has: battery, volume, brightness, auto sleep, do not disturb, name, details. */
function GlassesControls({ hub }: { hub: HubState }) {
  const device = hub.device
  const [name, setName] = useState(hub.prefs.name)
  useEffect(() => setName(hub.prefs.name), [hub.prefs.name])
  const on = hub.connected && hub.cameras > 0
  const about = [
    ["Model", device.model], ["Software", device.firmware], ["Maverick on glasses", device.app],
    ["Wi-Fi address", device.ip],
  ].filter(([, value]) => value)
  return (
    <section className="mt-7">
      <h2 className="px-1 text-sm text-muted-foreground">Your glasses</h2>
      <Card className="mt-2 divide-y divide-white/[0.06]">
        <div className="flex items-center gap-3.5 py-4">
          <span className="grid size-11 shrink-0 place-items-center rounded-full bg-white/[0.06] [&_svg]:size-5"><Glasses /></span>
          <div className="min-w-0 flex-1">
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              onBlur={() => name.trim() !== hub.prefs.name && setPrefs({ name: name.trim() })}
              onKeyDown={(event) => event.key === "Enter" && (event.target as HTMLInputElement).blur()}
              aria-label="Glasses name" maxLength={40}
              className="w-full bg-transparent text-[17px] font-semibold outline-none"
            />
            <p className="mt-0.5 text-[13px] text-muted-foreground">
              {!on ? "Not connected" : hub.asleep ? "Asleep. Tap the glasses to wake them." : "Connected"}
            </p>
          </div>
          {device.battery != null && (
            <span className={cn("flex items-center gap-1.5 text-sm", device.battery <= 20 && !device.charging ? "text-warn" : "text-foreground")}>
              {device.charging ? <BatteryCharging className="size-5 text-brand" /> : <BatteryMedium className="size-5" />}
              <span className="num">{device.battery}%</span>
            </span>
          )}
        </div>
        <WifiControls hub={hub} />
        <Brightness hub={hub} />
        <Slider icon={<Volume2 />} title="Volume" value={hub.prefs.volume} onChange={(volume) => setPrefs({ volume })} />
        <Row
          icon={<Cpu />} title="Find parts on the glasses"
          hint={hub.link === "wifi" && hub.detectBy === "glasses" ? `Always on over Wi-Fi: ${hub.glassesDetectFps.toFixed(1)} looks a second.`
            : hub.prefs.detect !== "glasses" && hub.link !== "wifi" ? "Runs the part model on the glasses themselves."
            : hub.detectBy === "glasses" ? `On the glasses: ${hub.glassesDetectFps.toFixed(1)} looks a second. Far fewer pictures leave them.`
            : "Starting. The laptop is covering until the glasses are ready."}
        >
          <Switch
            checked={hub.prefs.detect === "glasses" || hub.link === "wifi"} disabled={hub.link === "wifi"}
            onCheckedChange={(on) => { tap(); setPrefs({ detect: on ? "glasses" : "hub" }) }}
          />
        </Row>
        <Row icon={<Timer />} title="Turn off automatically" hint="Display and camera sleep when you stop building.">
          <select
            value={hub.prefs.sleep_after}
            onChange={(event) => setPrefs({ sleep_after: Number(event.target.value) })}
            aria-label="Turn off automatically"
            className="h-9 rounded-full bg-white/[0.06] px-3 text-sm font-medium outline-none"
          >
            <option value={10}>10 min</option>
            <option value={15}>15 min</option>
            <option value={30}>30 min</option>
            <option value={0}>Never</option>
          </select>
        </Row>
        <Row icon={<BellOff />} title="Do not disturb" hint="No sounds or speech. The lens still guides you.">
          <Switch checked={hub.prefs.dnd} onCheckedChange={(dnd) => setPrefs({ dnd })} />
        </Row>
        <Row icon={<Moon />} title={hub.asleep ? "Glasses are asleep" : "Sleep now"} hint={hub.asleep ? "They pick up where you left off." : "Turns the display and camera off."}>
          <Button variant="outline" size="sm" disabled={!on} onClick={() => { tap(); send({ type: "sleep", on: !hub.asleep }) }}>
            {hub.asleep ? "Wake" : "Sleep"}
          </Button>
        </Row>
        {about.length > 0 && (
          <dl className="grid gap-1.5 py-4 text-[13px]">
            {about.map(([label, value]) => (
              <div key={label} className="flex justify-between gap-4">
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="num truncate text-right">{value}</dd>
              </div>
            ))}
          </dl>
        )}
      </Card>
    </section>
  )
}

function GlassesStatus({ hub }: { hub: HubState }) {
  const on = hub.connected && hub.cameras > 0
  return (
    <span className="flex items-center gap-2 text-sm text-muted-foreground">
      <span className={cn("size-2 rounded-full", on ? "bg-brand" : "bg-white/25")} />
      {on ? (hub.link === "wifi" ? "Glasses on Wi-Fi" : "Glasses connected") : hub.connected ? "Glasses not connected" : "Reconnecting"}
    </span>
  )
}

function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <section className={cn("rounded-3xl border border-white/[0.08] bg-white/[0.03] px-5", className)}>{children}</section>
}

// ----- setup: four short screens, then the glasses take over -----
function Onboarding({ hub, onDone }: { hub: HubState; onDone: () => void }) {
  const [page, setPage] = useState(0)
  const [handing, setHanding] = useState(false)
  const next = () => { tap(); setPage((p) => p + 1) }

  const start = () => {
    tap()
    setHanding(true)
    send({ type: "start" })
    setTimeout(onDone, 1800)
  }

  if (handing) {
    return (
      <div className="grid min-h-dvh place-content-center justify-items-center gap-5 px-8 text-center">
        <span className="grid size-16 place-items-center rounded-full bg-brand text-brand-foreground"><Glasses className="size-7" /></span>
        <h1 className="text-[28px] font-semibold tracking-tight">Continue on your glasses</h1>
        <p className="text-muted-foreground">Your build is starting on the lens. You can put the phone down.</p>
      </div>
    )
  }

  const pages = [
    {
      title: "Build with both hands free",
      body: "Maverick shows the next part on your glasses, checks your work as you go and answers when you ask.",
      content: (
        <ul className="grid gap-4 text-[15px]">
          {[
            "A green box marks the part to pick up and where it goes",
            "It sees when a step is done, so you never tap next",
            "Tap and ask “where is the red rear?” and it points the way",
          ].map((line) => (
            <li key={line} className="flex gap-3"><Check className="mt-0.5 size-5 shrink-0 text-brand" />{line}</li>
          ))}
        </ul>
      ),
      action: <Button variant="brand" size="xl" className="w-full" onClick={() => { send({ type: "onboarding" }); next() }}>Get started</Button>,
    },
    {
      title: "You decide what Maverick sees",
      body: "Maverick needs the glasses' camera to check your work. You can change any of this later, or pause the camera at any moment.",
      content: (
        <Card className="divide-y divide-white/[0.06]">
          <Row icon={<Camera />} title="Camera" hint="Pictures of your workspace are checked, then discarded.">
            <Switch checked={hub.prefs.camera} onCheckedChange={(camera) => setPrefs({ camera })} />
          </Row>
          <Row icon={<Mic />} title="Microphone" hint="Listens when you tap the glasses, or when you say “Maverick”.">
            <Switch checked={hub.prefs.mic} onCheckedChange={(mic) => setPrefs({ mic })} />
          </Row>
          <Row title="Keep pictures from this build" hint="Off unless you turn it on. Helps improve part recognition.">
            <Switch checked={hub.prefs.save} onCheckedChange={(save) => setPrefs({ save })} />
          </Row>
        </Card>
      ),
      action: <Button variant="brand" size="xl" className="w-full" disabled={!hub.prefs.camera} onClick={next}>{hub.prefs.camera ? "Agree and continue" : "Camera is needed to continue"}</Button>,
    },
    {
      title: "Fit it to you",
      body: "The overlay is lined up for your dominant eye. To find it: point at something far away with both eyes open, then close one eye. The eye that keeps your finger on target is dominant.",
      content: (
        <Card className="divide-y divide-white/[0.06]">
          <Row icon={<Eye />} title="Dominant eye">
            <Pick value={hub.eye} options={[["left", "Left"], ["right", "Right"]]} onChange={setEye} />
          </Row>
          <Brightness hub={hub} />
          <Row icon={<Volume2 />} title="Spoken guidance" hint="Each step is read out in your ear.">
            <Switch checked={hub.prefs.voice} onCheckedChange={(voice) => setPrefs({ voice })} />
          </Row>
          <Row title="How much help" hint={hub.prefs.guidance === "full" ? "Live hints while you work." : "Only warnings interrupt you."}>
            <Pick value={hub.prefs.guidance} options={[["full", "Full"], ["minimal", "Minimal"]]} onChange={(guidance) => setPrefs({ guidance })} />
          </Row>
        </Card>
      ),
      action: <Button variant="brand" size="xl" className="w-full" onClick={next}>Continue</Button>,
    },
    {
      title: "Ready when you are",
      body: "Put your glasses on. The build starts on the lens, and this phone keeps your place.",
      content: (
        <Card className="py-1">
          <Row icon={<Glasses />} title={hub.title || "Your build"} hint={`${hub.steps.length} steps`}>
            <GlassesStatus hub={hub} />
          </Row>
        </Card>
      ),
      action: <Button variant="brand" size="xl" className="w-full" onClick={start}>Start on glasses <ChevronRight /></Button>,
    },
  ]
  const current = pages[page]

  return (
    <div className="flex min-h-dvh flex-col px-6 pb-8 pt-6">
      <div className="flex items-center justify-between">
        <span className="text-lg font-semibold tracking-tight">Maverick</span>
        <div className="flex gap-1.5" aria-label={`Step ${page + 1} of ${pages.length}`}>
          {pages.map((_, i) => (
            <span key={i} className={cn("h-1.5 rounded-full transition-all", i === page ? "w-6 bg-brand" : i < page ? "w-1.5 bg-white/60" : "w-1.5 bg-white/20")} />
          ))}
        </div>
      </div>
      <div className="mt-12 flex-1">
        <h1 className="text-[32px] font-semibold leading-[1.1] tracking-tight">{current.title}</h1>
        <p className="mt-3 text-[15px] leading-relaxed text-muted-foreground">{current.body}</p>
        <div className="mt-8">{current.content}</div>
      </div>
      <div className="grid gap-2">
        {current.action}
        {page > 0 && <Button variant="ghost" size="lg" onClick={() => { tap(); setPage(page - 1) }}>Back</Button>}
      </div>
    </div>
  )
}

// ----- while you build: the phone keeps your place -----
function time(seconds: number) {
  return seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${seconds % 60} s`
}

function Home({ hub, onSetup }: { hub: HubState; onSetup: () => void }) {
  const step = hub.steps[hub.index]
  const total = hub.steps.length
  const paused = !hub.prefs.camera
  return (
    <div className="mx-auto max-w-xl px-5 pb-10 pt-6">
      <header className="flex items-center justify-between">
        <span className="text-lg font-semibold tracking-tight">Maverick</span>
        <GlassesStatus hub={hub} />
      </header>

      <Card className="mt-6 py-5">
        <p className="text-sm text-muted-foreground">
          {hub.finished ? "Finished" : hub.asleep ? "Glasses asleep" : hub.stage === "setup" ? "Starting on your glasses" : paused ? "Camera paused" : "On your glasses now"}
        </p>
        {hub.finished ? (
          <>
            <h1 className="mt-1.5 text-[26px] font-semibold leading-tight tracking-tight">{hub.title} is built</h1>
            <div className="mt-5 grid grid-cols-3 gap-2 text-center">
              {[[time(hub.run.seconds), "Build time"], [String(hub.run.caught), "Slips caught"], [String(hub.run.asked), "Questions"]].map(([value, label]) => (
                <div key={label} className="rounded-2xl bg-white/[0.05] px-2 py-3">
                  <p className="num text-lg font-semibold">{value}</p>
                  <p className="text-xs text-muted-foreground">{label}</p>
                </div>
              ))}
            </div>
          </>
        ) : (
          <>
            <div className="mt-2 flex items-center gap-4">
              {step?.cls && <img src={`/guides/kits/demo/parts/${step.cls}.png`} alt="" className="size-16 shrink-0 rounded-2xl bg-black object-contain" />}
              <div className="min-w-0">
                <h1 className="text-[22px] font-semibold leading-tight tracking-tight">{step?.say ?? "Waiting for the build"}</h1>
                <p className="num mt-1 text-sm text-muted-foreground">Step {hub.index + 1} of {total}</p>
              </div>
            </div>
            {hub.screen.warn ? (
              <p className="mt-4 flex items-center gap-2 rounded-2xl bg-warn/10 px-3.5 py-2.5 text-sm text-warn"><TriangleAlert className="size-4 shrink-0" />{hub.screen.big}</p>
            ) : hub.hint.text ? (
              <p className={cn("mt-4 rounded-2xl bg-white/[0.05] px-3.5 py-2.5 text-sm", hub.hint.tone === "ok" ? "text-brand" : "text-foreground/85")}>{hub.hint.text}</p>
            ) : null}
          </>
        )}
        <div className="mt-5 flex gap-1.5">
          {hub.steps.map((_, i) => (
            <span key={i} className={cn("h-1.5 flex-1 rounded-full", hub.finished || i < hub.index ? "bg-brand" : i === hub.index ? "bg-white" : "bg-white/15")} />
          ))}
        </div>
      </Card>

      {hub.feed.length > 0 && (
        <section className="mt-7">
          <h2 className="px-1 text-sm text-muted-foreground">What Maverick noticed</h2>
          <ul className="mt-2 grid gap-2">
            {hub.feed.map((item) => (
              <li key={item.at} className="flex items-start gap-3 rounded-2xl bg-white/[0.04] px-4 py-3 text-sm">
                <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", item.tone === "warn" ? "bg-warn" : item.tone === "ok" ? "bg-brand" : "bg-white/50")} />
                {item.text}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="mt-7">
        <h2 className="px-1 text-sm text-muted-foreground">Yours to control</h2>
        <Card className="mt-2 divide-y divide-white/[0.06]">
          <Row icon={<Camera />} title="Camera" hint={paused ? "Paused. Nothing is being looked at." : "Checking your work. Pictures are not kept."}>
            <Switch checked={hub.prefs.camera} onCheckedChange={(camera) => { tap(); setPrefs({ camera }) }} />
          </Row>
          <Row icon={<Mic />} title="Microphone" hint="Tap the glasses to talk, or say “Maverick” first.">
            <Switch checked={hub.prefs.mic} onCheckedChange={(mic) => setPrefs({ mic })} />
          </Row>
          <Row icon={<Volume2 />} title="Spoken guidance">
            <Switch checked={hub.prefs.voice} onCheckedChange={(voice) => setPrefs({ voice })} />
          </Row>
          <Row title="How much help" hint={hub.prefs.guidance === "full" ? "Live hints while you work." : "Only warnings interrupt you."}>
            <Pick value={hub.prefs.guidance} options={[["full", "Full"], ["minimal", "Minimal"]]} onChange={(guidance) => setPrefs({ guidance })} />
          </Row>
          <Row icon={<Eye />} title="Dominant eye">
            <Pick value={hub.eye} options={[["left", "Left"], ["right", "Right"]]} onChange={setEye} />
          </Row>
          <Row title="Keep pictures from this build" hint="Off unless you turn it on.">
            <Switch checked={hub.prefs.save} onCheckedChange={(save) => setPrefs({ save })} />
          </Row>
        </Card>
      </section>

      <GlassesControls hub={hub} />

      <div className="mt-7 grid gap-2">
        <Button variant="outline" size="lg" onClick={() => { tap(); send({ type: "start" }) }}><RotateCcw /> Restart the build</Button>
        <Button variant="ghost" size="lg" onClick={() => { tap(); onSetup() }}>Run setup again</Button>
      </div>
    </div>
  )
}

export default function Phone() {
  const hub = useHub()
  const [setup, setSetup] = useState<boolean | null>(null)
  useEffect(connect, [])
  useEffect(() => { document.title = "Maverick" }, [])
  // Decide once, when the hub first tells us whether this wearer has been set up.
  useEffect(() => { if (hub.ready && setup === null) setSetup(!hub.prefs.onboarded) }, [hub.ready, hub.prefs.onboarded, setup])

  if (setup === null) {
    return (
      <div className="grid min-h-dvh place-content-center justify-items-center gap-2 text-center">
        <p className="text-[28px] font-semibold tracking-tight">Maverick</p>
        <p className="text-sm text-muted-foreground">Connecting…</p>
      </div>
    )
  }
  return setup
    ? <Onboarding hub={hub} onDone={() => setSetup(false)} />
    : <Home hub={hub} onSetup={() => { setPrefs({ onboarded: false }); setSetup(true) }} />
}
