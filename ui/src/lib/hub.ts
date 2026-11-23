/**
 * One connection to the Maverick hub (WebSocket /ws?role=monitor).
 *
 * Slow-changing things (step, hint, switches) live in a small store that React reads.
 * Fast things (camera frames, AR boxes, detections) are kept out of React: the live view
 * reads them straight from `live` inside its animation frame, so 8 frames a second never
 * re-render the page.
 */
import { useSyncExternalStore } from "react"

export type Box = [number, number, number, number]

export type Step = {
  id: number
  say: string
  piece: string
  cls: string
  find: string
  place: string
  check: string
  detail: string
  done_refs: string[]
}

export type Target = { id: string; box: Box; label: string; sub: string; style: "pick" | "place" | "remove" }
export type Detection = { cls: string; conf: number; box: Box }
/** A part the wearer asked about that is out of view: offset from the view's centre. */
export type Away = { label: string; dx: number; dy: number }

export type Screen = {
  mode: "step" | "info" | "choice"
  options?: string[]
  selected?: number
  top: string
  big: string
  sub: string
  warn: boolean
  progress: number
  step?: number
  total?: number
  icon?: string
  image?: string
  peek_ms?: number
  flash?: string
  sound?: string
  say_url?: string
}

export type Hint = { text: string; tone: "info" | "ok" | "warn" }

export type Prefs = {
  brightness: number; voice: boolean; guidance: "full" | "minimal"
  camera: boolean; mic: boolean; save: boolean; onboarded: boolean
  volume: number; sleep_after: number; dnd: boolean; name: string
  detect: "hub" | "glasses"
}
export type Device = {
  battery?: number; charging?: boolean; volume?: number; model?: string
  firmware?: string; app?: string; ip?: string; rssi?: number
  link?: "usb" | "wifi"
  wifi?: { on?: boolean; connected?: boolean; ssid?: string; ip?: string; rssi?: number; state?: string }
}
export type FeedItem = { at: number; tone: "ok" | "warn" | "info"; text: string }

export type HubState = {
  connected: boolean
  ready: boolean
  prefs: Prefs
  run: { seconds: number; caught: number; asked: number }
  feed: FeedItem[]
  device: Device
  asleep: boolean
  link: "" | "usb" | "wifi"
  addresses: string[]
  detectBy: "hub" | "glasses"
  glassesDetectMs: number
  glassesDetectFps: number
  title: string
  steps: Step[]
  index: number
  finished: boolean
  cameras: number
  cameraSize: string
  checks: boolean
  boxes: boolean
  detector: boolean
  detectMs: number
  checkMs: number
  model: string
  calib: Box
  calibrating: boolean
  calibError: string
  stage: "setup" | "run"
  eye: "left" | "right"
  screen: Screen
  hint: Hint
  flashAt: number
  flashKind: string
  peekUntil: number
  seen: string
  phase: string
  verdict: string
  heard: string
  /** The lens's push-to-talk pill: "Listening…" after a tap, "Thinking…" until the answer. */
  ptt: "off" | "listening" | "thinking"
  /** The lens's listening dot: something said hands-free is with the hub. */
  micWaiting: boolean
  /** The browser will not play sound until the page has been clicked once. */
  soundBlocked: boolean
  found: Detection[]
  note: string
  hasFrame: boolean
}

const blank: Screen = { mode: "info", top: "", big: "", sub: "", warn: false, progress: 0 }

let state: HubState = {
  connected: false, ready: false,
  prefs: {
    brightness: 1, voice: true, guidance: "full", camera: true, mic: true, save: false, onboarded: false,
    volume: 100, sleep_after: 30, dnd: false, name: "Maverick glasses", detect: "hub",
  },
  device: {}, asleep: false, link: "", addresses: [], detectBy: "hub", glassesDetectMs: 0, glassesDetectFps: 0,
  run: { seconds: 0, caught: 0, asked: 0 }, feed: [], title: "", steps: [], index: 0, finished: false, cameras: 0,
  cameraSize: "", checks: true, boxes: true, detector: false, detectMs: 0, checkMs: 0,
  model: "", calib: [0.33, 0.3, 0.67, 0.64], calibrating: false, calibError: "", stage: "run", eye: "right",
  screen: blank, hint: { text: "", tone: "info" }, flashAt: 0, flashKind: "done", peekUntil: 0, seen: "",
  phase: "", verdict: "", heard: "", ptt: "off", micWaiting: false, soundBlocked: false, found: [], note: "", hasFrame: false,
}

/** Read every animation frame by the live view; never triggers a render. */
export const live = {
  frame: null as string | null,   // data URL of the newest camera frame
  frameSeq: 0,
  targets: [] as Target[],
  away: [] as Away[],
  targetsAt: 0,
  found: [] as Detection[],
  foundAt: 0,
}

const listeners = new Set<() => void>()
function set(patch: Partial<HubState>) {
  state = { ...state, ...patch }
  listeners.forEach((l) => l())
}
export function useHub(): HubState {
  return useSyncExternalStore(
    (l) => { listeners.add(l); return () => listeners.delete(l) },
    () => state,
  )
}

let socket: WebSocket | null = null
let audio: HTMLAudioElement | null = null
let foundShownAt = 0

/** The lens's sounds on this Mac too: on by default on the display page, never in the phone app. */
export function voiceOn(): boolean {
  if (location.pathname.startsWith("/app")) return false
  try { return localStorage.getItem("voice") !== "0" } catch { return true }
}
export function setVoice(on: boolean) {
  try { localStorage.setItem("voice", on ? "1" : "0") } catch { /* private window */ }
  if (!on && audio) audio.pause()
  if (on) unlockSound()
  set({})
}

// Browsers stay silent until the page has been clicked or a key pressed. Until then the
// page says so; the first click or key turns the sound on and plays the clip that was held.
let tones: AudioContext | null = null
let held = ""
function toneContext(): AudioContext | null {
  try { tones ??= new AudioContext() } catch { return null }
  return tones
}
function unlockSound() {
  const context = voiceOn() ? toneContext() : null
  if (!context) return
  void context.resume().then(() => {
    if (state.soundBlocked) set({ soundBlocked: false })
    if (held) { const url = held; held = ""; say(url) }
  }).catch(() => {})
}
let watching = false
function watchSound() {
  watching = true
  const context = voiceOn() ? toneContext() : null
  if (context && context.state !== "running") set({ soundBlocked: true })
  window.addEventListener("pointerdown", unlockSound)
  window.addEventListener("keydown", unlockSound)
}

function say(url?: string) {
  if (!url || !voiceOn()) return
  if (audio) audio.pause()
  held = ""
  const clip = new Audio(url)
  audio = clip
  clip.play().catch(() => {  // not clicked yet: keep the newest clip for the first click
    if (audio !== clip) return
    held = url
    set({ soundBlocked: true })
  })
}
function hush() {
  held = ""
  if (audio) audio.pause()
}

/** The lens's tones: step done (two short high notes), something wrong (a low chord), tap to talk (a blip). */
function beep(sound?: string) {
  if (!sound || !voiceOn()) return
  const context = toneContext()
  if (!context || context.state !== "running") return
  const note = (hz: number[], at: number, seconds: number, level: number) => {
    const gain = context.createGain()
    const start = context.currentTime + at
    gain.gain.setValueAtTime(0, start)
    gain.gain.linearRampToValueAtTime(level / hz.length, start + 0.008)
    gain.gain.setValueAtTime(level / hz.length, start + seconds - 0.012)
    gain.gain.linearRampToValueAtTime(0, start + seconds)
    gain.connect(context.destination)
    for (const f of hz) {
      const osc = context.createOscillator()
      osc.frequency.value = f
      osc.connect(gain)
      osc.start(start)
      osc.stop(start + seconds)
    }
  }
  if (sound === "done") { note([1200], 0, 0.1, 0.16); note([1200], 0.2, 0.1, 0.16) }
  else if (sound === "warn") note([300, 400, 500], 0, 0.35, 0.2)
  else if (sound === "blip") note([400, 1200], 0, 0.045, 0.07)
}

// The lens drops "Listening…" by itself (nothing said, or six seconds), and "Thinking…" and
// its dot when no answer comes. The hub is not told, so the page keeps the same clocks.
let pttTimer = 0
let micTimer = 0
function setPtt(value: HubState["ptt"]) {
  clearTimeout(pttTimer)
  if (value !== "off") pttTimer = window.setTimeout(() => set({ ptt: "off" }), value === "listening" ? 6800 : 4000)
  if (state.ptt !== value) set({ ptt: value })
}
function setMicWaiting(on: boolean) {
  clearTimeout(micTimer)
  if (on) micTimer = window.setTimeout(() => set({ micWaiting: false }), 6000)
  if (state.micWaiting !== on) set({ micWaiting: on })
}

export function send(message: Record<string, unknown>) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message))
}
export const command = (name: string) => send({ type: "command", name })
export const setPrefs = (values: Partial<Prefs>) => {
  set({ prefs: { ...state.prefs, ...values } })  // show it at once; the hub confirms
  send({ type: "prefs", values })
}
export const setEye = (value: "left" | "right") => { set({ eye: value }); send({ type: "eye", value }) }

/** What Maverick noticed, newest first: shown in the phone app. */
function note(tone: FeedItem["tone"], text: string): FeedItem[] {
  if (!text || state.feed[0]?.text === text) return state.feed
  return [{ at: Date.now(), tone, text }, ...state.feed].slice(0, 6)
}

function handle(m: any) {
  switch (m.type) {
    case "frame":
      live.frame = "data:image/jpeg;base64," + m.jpeg
      live.frameSeq++
      if (!state.hasFrame) set({ hasFrame: true })
      break
    case "ar":
      live.targets = m.targets ?? []
      live.away = m.away ?? []
      live.targetsAt = performance.now()
      break
    case "dets": {
      live.found = m.found ?? []
      live.foundAt = performance.now()
      const now = performance.now()
      if (now - foundShownAt > 300) {  // the settings panel only needs a few updates a second
        foundShownAt = now
        set({ found: live.found, detectMs: m.ms ?? state.detectMs })
      }
      break
    }
    case "screen": {
      const now = performance.now()
      set({
        feed: m.warn ? note("warn", m.sub ? `${m.big}: ${m.sub.toLowerCase()}` : m.big)
          : m.flash === "done" && m.step ? note("ok", `Step ${m.step - 1} checked and done`)
          : m.flash === "done" ? note("ok", "Build finished")
          : m.mode === "step" && m.step === 1 ? [] : state.feed,
        screen: m, hint: { text: "", tone: "info" }, seen: "",
        flashAt: m.flash ? now : state.flashAt,
        flashKind: m.flash ?? state.flashKind,
        peekUntil: m.peek_ms && m.image ? now + m.peek_ms : 0,
      })
      if (state.ptt === "thinking") setPtt("off")
      beep(m.flash === "done" ? "done" : m.flash === "wrong" ? "warn" : m.sound)
      say(m.say_url)
      break
    }
    case "hint":
      set({
        hint: { text: m.text ?? "", tone: m.tone ?? "info" },
        peekUntil: m.peek_ms && state.screen.image ? performance.now() + m.peek_ms : state.peekUntil,
      })
      if (state.ptt === "thinking") setPtt("off")
      setMicWaiting(false)
      say(m.say_url)
      break
    case "status":
      if (m.verdict) {
        // "Nothing to see" is not worth a line on the page.
        const quiet = m.phase === "no_build" || m.phase === "done"
        set({ seen: quiet ? "" : m.seen ?? "", phase: m.phase ?? "", verdict: m.verdict, checkMs: m.ms ?? 0 })
      }
      break
    case "guide":
      set({ title: m.title ?? "", steps: m.steps ?? [] })
      break
    case "state":
      set({
        index: m.index, finished: m.finished, cameras: m.glasses, cameraSize: m.camera ?? "",
        checks: m.auto, boxes: m.ar, detector: !!m.detector, model: m.model ?? "",
        detectMs: m.detect_ms || state.detectMs,
        calib: m.calib?.length === 4 ? m.calib : state.calib, calibrating: !!m.calibrating,
        stage: m.stage === "setup" ? "setup" : "run", eye: m.eye === "left" ? "left" : "right",
        ready: true, prefs: m.prefs ?? state.prefs, run: m.run ?? state.run,
        device: m.device ?? state.device, asleep: !!m.asleep,
        detectBy: m.detect_by === "glasses" ? "glasses" : "hub",
        link: m.link ?? "", addresses: m.addresses ?? [],
        glassesDetectMs: m.glasses_detect_ms ?? 0, glassesDetectFps: m.glasses_detect_fps ?? 0,
      })
      break
    case "calib":
      set({ calibrating: !!m.on, calibError: m.error ?? "", note: m.ok ? "Lens calibrated" : state.note })
      break
    case "heard":
      if (m.kind === "listening") {  // a tap on the touchpad: the lens stops talking and listens
        hush()
        beep("blip")
        setPtt("listening")
        break
      }
      if (m.kind === "thinking") { setPtt("thinking"); break }
      if (m.kind === "waiting") { setMicWaiting(true); break }
      set({
        heard: m.text ?? "",
        feed: m.kind === "ask" ? note("info", `You asked: ${m.value}`) : state.feed,
      })
      break
    case "parse":
      set({ note: m.error ? m.error : "" })
      break
  }
}

export function connect() {
  if (socket) return
  const scheme = location.protocol === "https:" ? "wss" : "ws"
  // The phone app follows the build without the camera pictures.
  const lite = location.pathname.startsWith("/app") ? "&lite=1" : ""
  socket = new WebSocket(`${scheme}://${location.host}/ws?role=monitor${lite}`)
  socket.onopen = () => set({ connected: true })
  if (!watching) watchSound()
  socket.onmessage = (event) => handle(JSON.parse(event.data))
  socket.onclose = () => {
    socket = null
    set({ connected: false })
    setTimeout(connect, 1000)
  }
}

// ----- the demo's steps (edited in Settings) -----
export async function api<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method, headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await response.json()
  if (!response.ok) throw new Error(data.error ?? response.statusText)
  return data as T
}

// ----- this Mac's camera, as a stand-in when no glasses or phone is connected -----
let macTimer: number | null = null
let macStream: MediaStream | null = null
export const macCameraOn = () => macTimer !== null
export async function toggleMacCamera(): Promise<void> {
  if (macTimer !== null) {
    clearInterval(macTimer)
    macTimer = null
    macStream?.getTracks().forEach((t) => t.stop())
    macStream = null
    set({})
    return
  }
  macStream = await navigator.mediaDevices.getUserMedia({
    video: { width: { ideal: 1280 }, height: { ideal: 960 } },
  })
  const video = document.createElement("video")
  video.muted = true
  video.playsInline = true
  video.srcObject = macStream
  await video.play()
  const canvas = document.createElement("canvas")
  let seq = 0
  macTimer = window.setInterval(() => {
    canvas.width = video.videoWidth || 1280
    canvas.height = video.videoHeight || 960
    canvas.getContext("2d")!.drawImage(video, 0, 0, canvas.width, canvas.height)
    send({ type: "frame", seq: ++seq, jpeg: canvas.toDataURL("image/jpeg", 0.7).split(",")[1] })
  }, 140)
  set({})
}
