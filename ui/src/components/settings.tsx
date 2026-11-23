import { useEffect, useState } from "react"
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Minus, Plus, Settings2 } from "lucide-react"

import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Separator } from "@/components/ui/separator"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { api, command, macCameraOn, send, setVoice, toggleMacCamera, useHub, voiceOn, type Step, setPrefs } from "@/lib/hub"

function Row({ id, title, hint, children }: { id?: string; title: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-6">
      <div className="min-w-0">
        <Label htmlFor={id} className="text-[15px] font-medium">{title}</Label>
        {hint && <p className="mt-0.5 text-sm text-muted-foreground">{hint}</p>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-4">
      <h3 className="text-sm text-muted-foreground">{title}</h3>
      {children}
    </section>
  )
}

type Guide = { title: string; steps: Step[] }

function StepEditor() {
  const [guide, setGuide] = useState<Guide | null>(null)
  const [note, setNote] = useState("")
  useEffect(() => {
    api<Guide>("/api/demo").then(setGuide).catch((error) => setNote(error.message))
  }, [])
  if (!guide) return <p className="text-sm text-muted-foreground">{note || "Loading…"}</p>

  const change = (index: number, field: keyof Step, value: string) =>
    setGuide({ ...guide, steps: guide.steps.map((s, i) => (i === index ? { ...s, [field]: value } : s)) })
  const save = async () => {
    try {
      setGuide(await api<Guide>("/api/demo", "PUT", guide))
      setNote("Saved")
    } catch (error) {
      setNote((error as Error).message)
    }
  }

  return (
    <div className="space-y-3">
      <Accordion type="single" collapsible>
        {guide.steps.map((step, i) => (
          <AccordionItem key={i} value={String(i)}>
            <AccordionTrigger className="text-[15px]">{step.say || `Step ${i + 1}`}</AccordionTrigger>
            <AccordionContent className="space-y-3">
              <div className="space-y-1.5">
                <Label htmlFor={`say-${i}`}>Instruction</Label>
                <Input id={`say-${i}`} value={step.say} onChange={(e) => change(i, "say", e.target.value)} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor={`piece-${i}`}>Part name</Label>
                <Input id={`piece-${i}`} value={step.piece} onChange={(e) => change(i, "piece", e.target.value)} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor={`place-${i}`}>Where it goes</Label>
                <Textarea id={`place-${i}`} value={step.place} onChange={(e) => change(i, "place", e.target.value)} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor={`check-${i}`}>What done looks like</Label>
                <Textarea id={`check-${i}`} value={step.check} onChange={(e) => change(i, "check", e.target.value)} />
              </div>
            </AccordionContent>
          </AccordionItem>
        ))}
      </Accordion>
      <div className="flex items-center gap-3">
        <Button size="sm" variant="outline" onClick={save}>Save steps</Button>
        <span className="text-sm text-muted-foreground" role="status">{note}</span>
      </div>
    </div>
  )
}

export function Settings({ phoneView, onPhoneView }: { phoneView: boolean; onPhoneView: (on: boolean) => void }) {
  const hub = useHub()
  const nudge = (kind: string) => {
    let [x0, y0, x1, y1] = hub.calib
    const d = 0.005
    if (kind === "left") { x0 -= d; x1 -= d }
    if (kind === "right") { x0 += d; x1 += d }
    if (kind === "up") { y0 -= d; y1 -= d }
    if (kind === "down") { y0 += d; y1 += d }
    if (kind === "grow") { x0 -= d; x1 += d; y0 -= d * 0.75; y1 += d * 0.75 }
    if (kind === "shrink") { x0 += d; x1 -= d; y0 += d * 0.75; y1 -= d * 0.75 }
    send({ type: "calib", action: "set", rect: [x0, y0, x1, y1] })
  }

  return (
    <Sheet>
      <SheetTrigger asChild>
        <Button variant="outline" size="sm"><Settings2 /> Settings</Button>
      </SheetTrigger>
      <SheetContent className="overflow-y-auto sm:max-w-md">
        <SheetHeader>
          <SheetTitle className="text-xl">Settings</SheetTitle>
          <SheetDescription>Everything here is for the operator. The wearer never sees it.</SheetDescription>
        </SheetHeader>
        <div className="space-y-8 px-4 pb-10">
          <Section title="Display">
            <Row title="Show the view from" hint="Glasses crops to the area the lens covers.">
              <ToggleGroup
                type="single" variant="outline" size="sm"
                value={phoneView ? "phone" : "glasses"}
                onValueChange={(value) => value && onPhoneView(value === "phone")}
              >
                <ToggleGroupItem value="phone" className="rounded-full px-3.5 data-[state=on]:border-transparent data-[state=on]:bg-primary data-[state=on]:text-primary-foreground">Phone</ToggleGroupItem>
                <ToggleGroupItem value="glasses" className="rounded-full px-3.5 data-[state=on]:border-transparent data-[state=on]:bg-primary data-[state=on]:text-primary-foreground">Glasses</ToggleGroupItem>
              </ToggleGroup>
            </Row>
            <Row
              title="Find parts on the glasses"
              hint={hub.prefs.detect !== "glasses" ? `On this Mac now, ${hub.detectMs} ms a frame.`
                : hub.detectBy === "glasses" ? `On the glasses: ${hub.glassesDetectMs} ms a look, ${hub.glassesDetectFps.toFixed(1)} a second.`
                : "Waiting for the glasses; this Mac is covering."}
            >
              <Switch
                checked={hub.prefs.detect === "glasses"}
                onCheckedChange={(on) => setPrefs({ detect: on ? "glasses" : "hub" })}
              />
            </Row>
            <Row title="Dominant eye" hint="Picked in the phone app. Each eye keeps its own lens alignment.">
              <ToggleGroup
                type="single" variant="outline" size="sm"
                value={hub.eye}
                onValueChange={(value) => value && send({ type: "eye", value })}
              >
                <ToggleGroupItem value="left" className="rounded-full px-3.5 data-[state=on]:border-transparent data-[state=on]:bg-primary data-[state=on]:text-primary-foreground">Left</ToggleGroupItem>
                <ToggleGroupItem value="right" className="rounded-full px-3.5 data-[state=on]:border-transparent data-[state=on]:bg-primary data-[state=on]:text-primary-foreground">Right</ToggleGroupItem>
              </ToggleGroup>
            </Row>
            <Row id="voice" title="Play the voice on this Mac">
              <Switch id="voice" checked={voiceOn()} onCheckedChange={setVoice} />
            </Row>
          </Section>

          <Separator />
          <Section title="Assistance">
            <Row id="boxes" title="Highlight parts" hint="The green box on the part to pick up.">
              <Switch id="boxes" checked={hub.boxes} onCheckedChange={(on) => send({ type: "ar", on })} />
            </Row>
            <Row id="checks" title="Check steps automatically" hint="Moves on when the camera sees the step is done.">
              <Switch id="checks" checked={hub.checks} onCheckedChange={(on) => send({ type: "auto", on })} />
            </Row>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="outline" onClick={() => send({ type: "debug", verdict: "done" })}>Mark step done</Button>
              <Button size="sm" variant="outline" onClick={() => command("restart")}>Start over</Button>
            </div>
          </Section>

          <Separator />
          <Section title="Camera">
            <Row title="Use this Mac's camera" hint="A stand-in when no glasses or phone is connected.">
              <Switch checked={macCameraOn()} onCheckedChange={() => void toggleMacCamera().catch(() => {})} aria-label="Use this Mac's camera" />
            </Row>
          </Section>

          <Separator />
          <Section title="Lens alignment">
            <p className="text-sm text-muted-foreground">
              Glasses only. Lines the boxes up with the real parts. Takes one tap by the wearer.
            </p>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" variant="outline" onClick={() => send({ type: "calib", action: "start" })}>Align the lens</Button>
              <span className="mx-1 text-sm text-muted-foreground">Fine-tune</span>
              <Button size="icon-sm" variant="ghost" aria-label="Move left" onClick={() => nudge("left")}><ArrowLeft /></Button>
              <Button size="icon-sm" variant="ghost" aria-label="Move right" onClick={() => nudge("right")}><ArrowRight /></Button>
              <Button size="icon-sm" variant="ghost" aria-label="Move up" onClick={() => nudge("up")}><ArrowUp /></Button>
              <Button size="icon-sm" variant="ghost" aria-label="Move down" onClick={() => nudge("down")}><ArrowDown /></Button>
              <Button size="icon-sm" variant="ghost" aria-label="Smaller" onClick={() => nudge("shrink")}><Minus /></Button>
              <Button size="icon-sm" variant="ghost" aria-label="Larger" onClick={() => nudge("grow")}><Plus /></Button>
            </div>
          </Section>

          <Separator />
          <Section title="Steps">
            <StepEditor />
          </Section>

          <Separator />
          <Section title="Diagnostics">
            <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1.5 text-sm">
              <dt className="text-muted-foreground">Part detector</dt>
              <dd className="num">{hub.detector ? `On this Mac · ${hub.detectMs || "–"} ms` : "Cloud only"}</dd>
              <dt className="text-muted-foreground">Step check</dt>
              <dd className="num">{hub.model || "–"}{hub.checkMs ? ` · ${hub.checkMs} ms` : ""}</dd>
              <dt className="text-muted-foreground">Camera</dt>
              <dd className="num">{hub.cameras ? hub.cameraSize || "Connected" : "Not connected"}</dd>
              <dt className="text-muted-foreground">In view</dt>
              <dd>{hub.found.length ? hub.found.map((d) => `${d.cls} ${Math.round(d.conf * 100)}%`).join(", ") : "Nothing"}</dd>
              {hub.heard && (<><dt className="text-muted-foreground">Heard</dt><dd>{hub.heard}</dd></>)}
            </dl>
          </Section>
        </div>
      </SheetContent>
    </Sheet>
  )
}
