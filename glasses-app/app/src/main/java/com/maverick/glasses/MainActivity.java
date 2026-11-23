package com.maverick.glasses;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.ImageFormat;
import android.graphics.Matrix;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.Rect;
import android.graphics.RectF;
import android.graphics.SurfaceTexture;
import android.graphics.Typeface;
import android.graphics.YuvImage;
import android.graphics.drawable.GradientDrawable;
import android.hardware.Camera;
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.media.ToneGenerator;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.SystemClock;
import android.text.TextUtils;
import android.util.Base64;
import android.util.Log;
import android.util.LruCache;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.Surface;
import android.view.TextureView;
import android.view.View;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;

/**
 * Maverick glasses HUD: a light overlay. The lens stays black (transparent) except for a thin strip
 * along the bottom (rotating 3D brick, one instruction, live hint, step counter) and short-lived
 * things: a "peek" card with the manual picture, a check-mark flash, spoken audio.
 * Speaks the backend's JSON WebSocket protocol and streams camera frames as base64 JPEG.
 * Backend address: intent extra "url" (saved), else last saved, else ws://127.0.0.1:8000/ws (adb reverse).
 */
public class MainActivity extends Activity {
    private static final String TAG = "MaverickHUD";
    // The whole palette: white text, white at 70% for secondary text, one neon green accent, amber for warnings.
    private static final int WHITE = Color.WHITE;
    private static final int WHITE70 = Color.argb(179, 255, 255, 255);
    private static final int WHITE40 = Color.argb(102, 255, 255, 255);
    private static final int ACCENT = ArOverlayView.ACCENT;
    private static final int WARN = Color.rgb(0xFF, 0xC2, 0x66);
    private static final long DOUBLE_TAP_MS = 350;
    /** Where the instruction bar sits. Flip to false to put it back along the bottom edge. */
    private static final boolean BAR_AT_TOP = true;
    private static final int TAP_PEEK_MS = 5000;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final OkHttpClient http = new OkHttpClient();
    private volatile WebSocket socket;
    private volatile boolean socketOpen;
    private String url;
    private boolean alive;

    // Views
    private LinearLayout strip, info;
    private FrameLayout leftSlot;
    private BrickView brickView;
    private ImageView iconView, peek;
    private TextView big, line2, iBig, iSub;
    private View micDot;
    private DotsView dots;
    private DoneView flash;

    // Screen state (UI thread)
    private final LruCache<String, Bitmap> images = new LruCache<>(32);
    private final Set<String> loading = new HashSet<>();
    private String currentImage = "", currentIcon = "";
    private String subText = "", hintText = "", hintTone = "";
    private boolean hintActive;
    private long peekUntil;
    private long lastTapAt;

    // Camera (camera thread)
    private HandlerThread camThread;
    private Handler cam;
    private Camera camera;
    private SurfaceTexture dummyTexture;
    private int camW, camH;
    private volatile int cfgW = 640, cfgH = 480, cfgQuality = 60;
    private volatile long frameIntervalMs = 330;
    private final List<String> supportedSizes = new ArrayList<>();
    private volatile boolean resumed;
    private long lastFrameAt;
    private volatile long lastB64Len = 40_000;
    private long framesSent, bytesSent, statsAt;

    // Audio
    private ToneGenerator tones;
    private MediaPlayer player;
    private int sayToken;
    volatile boolean speaking;
    volatile long speakEndAt;
    private volatile long muteUntil;   // uptime until which the microphone ignores sound (our own audio)

    // Voice
    private VoiceCapture voice;

    // AR: boxes locked onto real objects
    private final BoxTracker tracker = new BoxTracker();
    private ArOverlayView overlay;
    private FrameLayout hud;                 // everything except the camera preview and the AR layer
    private TextureView previewView;         // phone mode only: live camera behind the HUD ("see-through")
    private volatile SurfaceTexture previewSurface;
    private boolean phoneMode, calibMode;
    private float dp = 1f;
    private long frameSeq;                   // counts every preview frame; sent as "seq" and quoted back in "ar"
    private int camRotation;                 // 180 when the sensor picture is upside down for this screen rotation
    private HandlerThread encThread;
    private Handler enc;                     // JPEG encoding runs here so the camera thread keeps every frame
    private byte[] encBuf;
    private volatile boolean encBusy;
    private long trackLogAt;

    // The part detector on the glasses (off unless the hub's config, or "-e detect glasses|bench", turns it on)
    private volatile Detector detector;
    private String detectForced = "";            // "-e detect glasses": on whatever the hub says (testing)
    private boolean detectWanted;                // the hub's config says "detect":"glasses"
    private int detectVersion = -1;              // the hub's "detect_version": the model it is serving now
    private boolean detectFailed;                // gave up; try again on a new version or a reconnect
    private volatile long lastCameraFrameAt;     // elapsedRealtime of the newest preview frame
    private long lastCameraRestartAt;
    private final Runnable cameraWatch = new Runnable() {
        @Override public void run() {
            long now = SystemClock.elapsedRealtime();
            if (!asleep && resumed && socketOpen && !calibMode
                    && now - lastCameraFrameAt > 5000 && now - lastCameraRestartAt > 5000) {
                lastCameraRestartAt = now;
                Log.w(TAG, "camera restarted (no frames for 5 s)");
                cam.post(() -> { closeCamera(); openCamera(); });
            }
            checkLink();
            ui.postDelayed(this, 1000);
        }
    };

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        setVolumeControlStream(AudioManager.STREAM_MUSIC);
        dp = getResources().getDisplayMetrics().density;
        Object previewExtra = getIntent().getExtras() == null ? null : getIntent().getExtras().get("preview");
        // Phone mode = any device that is not the glasses. "-e preview 1" forces it on, "-e preview 0" forces it off.
        phoneMode = previewExtra != null ? "1".equals(previewExtra.toString()) : !String.valueOf(Build.MODEL).contains("XNM");
        Log.i(TAG, "model " + Build.MODEL + " phoneMode " + phoneMode);
        // "--ez overlock true": draw above the lock screen and turn the screen on, for this launch only (not saved).
        // Lets a locked test phone show the HUD without anyone unlocking it; the lock itself stays in place.
        Object overlock = getIntent().getExtras() == null ? null : getIntent().getExtras().get("overlock");
        if (overlock != null && ("true".equals(overlock.toString()) || "1".equals(overlock.toString()))) {
            setShowWhenLocked(true);
            setTurnScreenOn(true);
            Log.i(TAG, "showing above the lock screen for this launch");
        }
        buildUi();
        android.content.IntentFilter events = new android.content.IntentFilter(android.content.Intent.ACTION_BATTERY_CHANGED);
        events.addAction("android.media.VOLUME_CHANGED_ACTION");
        events.addAction(android.net.wifi.WifiManager.WIFI_STATE_CHANGED_ACTION);
        events.addAction(android.net.wifi.WifiManager.NETWORK_STATE_CHANGED_ACTION);
        events.addAction(android.net.wifi.WifiManager.SUPPLICANT_STATE_CHANGED_ACTION);
        registerReceiver(deviceEvents, events);
        ui.postDelayed(deviceTick, 10000);
        lastCameraRestartAt = SystemClock.elapsedRealtime();   // give the first open its 5 s
        ui.postDelayed(cameraWatch, 1000);
        float savedBrightness = getSharedPreferences("h", MODE_PRIVATE).getFloat("brightness", -1f);
        if (savedBrightness > 0) setBrightness(savedBrightness, false);   // before any config arrives
        encThread = new HandlerThread("maverick-enc");
        encThread.start();
        enc = new Handler(encThread.getLooper());
        if (!phoneMode) {
            String saved = getSharedPreferences("h", MODE_PRIVATE).getString("calib", "");
            String[] parts = saved.split(",");
            if (parts.length == 4) {
                try {
                    overlay.setCalib(new float[]{Float.parseFloat(parts[0]), Float.parseFloat(parts[1]), Float.parseFloat(parts[2]), Float.parseFloat(parts[3])});
                } catch (Exception e) { Log.w(TAG, "bad saved calib " + saved); }
            }
        }
        camThread = new HandlerThread("maverick-cam");
        camThread.start();
        cam = new Handler(camThread.getLooper());
        try { tones = new ToneGenerator(AudioManager.STREAM_MUSIC, 80); } catch (Exception e) { Log.w(TAG, "no tones", e); }
        try {
            AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
            am.setStreamVolume(AudioManager.STREAM_MUSIC, am.getStreamMaxVolume(AudioManager.STREAM_MUSIC), 0);
        } catch (Exception e) { Log.w(TAG, "volume", e); }
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED
                || checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO}, 1);
        }
        voice = new VoiceCapture(new VoiceCapture.Listener() {
            @Override public void onUtterance(byte[] pcm, int rate, boolean ptt) {
                WebSocket ws = socket;
                if (ws == null || !socketOpen) { if (ptt) ui.post(() -> setPtt(PTT_OFF)); return; }
                ws.send("{\"type\":\"audio\",\"rate\":" + rate + (ptt ? ",\"ptt\":true" : "") + ",\"pcm16\":\"" + Base64.encodeToString(pcm, Base64.NO_WRAP) + "\"}");
                Log.i(TAG, "audio sent: " + (pcm.length / 2 * 1000L / rate) + " ms" + (ptt ? " (ptt)" : ""));
                ui.post(() -> { if (ptt) setPtt(PTT_THINKING); else setMic(MIC_WAITING); });
            }
            @Override public void onPttCancelled(String reason) {
                ui.post(() -> setPtt(PTT_OFF));
            }
            @Override public void onCapturing(boolean on) {
                ui.post(() -> { if (on) setMic(MIC_CAPTURING); else if (micState == MIC_CAPTURING) setMic(MIC_IDLE); });
            }
            @Override public boolean muted() {
                return speaking || SystemClock.uptimeMillis() < muteUntil;
            }
        });
        Object detectExtra = getIntent().getExtras() == null ? null : getIntent().getExtras().get("detect");
        detectForced = detectExtra == null ? "" : detectExtra.toString();
        handleIntent();
        updateDetector();
    }

    private static final String DETECT_META = "/guides/kits/demo/glasses/meta.json";

    // ---------- Boxes chosen on the glasses: detection to green box with no trip to the hub ----------

    private static final int GROUP_HUB = 0, GROUP_LOCAL = 1;

    /** What the hub wants shown for this step; the glasses find it themselves in their own detections. */
    private static final class Plan {
        int step; boolean show, hasHeading;
        String pickId, pickCls, pickLabel, placeId, placeLabel, where = "";
        String[] placeClasses = new String[0];
        float hx, hy, pickConf = 0.55f, keepConf = 0.35f;
    }

    /** Keeps one box steady: a new place must be seen twice before it is shown, and a miss is forgiven for a second. */
    private static final class Steady {
        static final int NONE = 0, ANCHOR = 1, KEEP = 2;
        float[] shown, cand;
        long shownAt;
        void reset() { shown = null; cand = null; shownAt = 0; }
        boolean recent(long now) { return shown != null && now - shownAt <= 1000; }
        private static boolean near(float[] a, float[] b) {
            float dx = (a[0] + a[2] - b[0] - b[2]) / 2, dy = (a[1] + a[3] - b[1] - b[3]) / 2;
            float reach = Math.max(0.08f, Math.max(b[2] - b[0], b[3] - b[1]));
            return dx * dx + dy * dy <= reach * reach;
        }
        /** @param box this detection's box for the target, or null when it was not found */
        int update(float[] box, long now) {
            if (box == null) {
                cand = null;
                if (shown != null && now - shownAt > 1000) shown = null;       // missed for over a second: gone
                return shown == null ? NONE : KEEP;
            }
            if (shown != null && near(box, shown)) { shown = box; shownAt = now; cand = null; return ANCHOR; }
            // somewhere new (or nothing shown yet): it has to be there twice in a row
            if (cand != null && near(box, cand)) { shown = box; shownAt = now; cand = null; return ANCHOR; }
            cand = box;
            if (shown != null && now - shownAt > 900) shown = null;            // the old place gets 0.9 s while the new one earns it
            return shown == null ? NONE : KEEP;
        }
    }

    private volatile boolean boxesLocal;         // the hub's config says "boxes":"glasses"
    private volatile Plan plan;
    private volatile int planEpoch;              // bumps whenever the local boxes are cleared
    private final Steady pickSteady = new Steady(), placeSteady = new Steady();   // detector thread only
    private int steadyEpoch = -1;
    private long localLogAt;

    /** {"type":"plan","step","show","pick":{id,cls,label},"place":{id,classes,label,where},"heading":[dx,dy],"pick_conf","keep_conf"} */
    private void applyPlan(JSONObject m) {
        Plan p = new Plan();
        p.step = m.optInt("step", 0);
        p.show = m.optBoolean("show", false);
        JSONObject pick = m.optJSONObject("pick"), place = m.optJSONObject("place");
        if (pick != null) { p.pickId = pick.optString("id", "pick"); p.pickCls = pick.optString("cls", ""); p.pickLabel = str(pick, "label"); }
        if (place != null) {
            p.placeId = place.optString("id", "place"); p.placeLabel = str(place, "label"); p.where = str(place, "where");
            JSONArray cs = place.optJSONArray("classes");
            p.placeClasses = new String[cs == null ? 0 : cs.length()];
            for (int i = 0; i < p.placeClasses.length; i++) p.placeClasses[i] = cs.optString(i);
        }
        JSONArray hd = m.optJSONArray("heading");
        if (hd != null && hd.length() == 2) { p.hasHeading = true; p.hx = (float) hd.optDouble(0, 0); p.hy = (float) hd.optDouble(1, 0); }
        p.pickConf = (float) m.optDouble("pick_conf", 0.55); p.keepConf = (float) m.optDouble("keep_conf", 0.35);
        Plan old = plan;
        plan = p;
        if (old == null || old.step != p.step || !p.show) clearLocalBoxes();     // a new step starts clean
    }

    /** Drops the boxes the glasses chose themselves, at once. Any thread. */
    private void clearLocalBoxes() {
        planEpoch++;
        cam.post(() -> {
            tracker.setGroup(GROUP_LOCAL, frameSeq, new String[0], new float[0][], new String[0], new String[0], new String[0], SystemClock.elapsedRealtime());
            overlay.setTargets(tracker.snapshot(), tracker.awaySnapshot());
        });
    }

    /** The part of the place box the plan asks for: the front or rear half, or the middle 30% along the car. */
    private static float[] subBox(float[] b, Plan p) {
        float x0 = b[0], y0 = b[1], x1 = b[2], y1 = b[3], w = x1 - x0, h = y1 - y0;
        if ("middle".equals(p.where)) {
            boolean alongX = p.hasHeading ? Math.abs(p.hx) * w >= Math.abs(p.hy) * h : w * 4 >= h * 3;
            return alongX ? new float[]{x0 + 0.35f * w, y0, x1 - 0.35f * w, y1} : new float[]{x0, y0 + 0.35f * h, x1, y1 - 0.35f * h};
        }
        if (("front".equals(p.where) || "rear".equals(p.where)) && p.hasHeading) {
            float vx = "rear".equals(p.where) ? p.hx : -p.hx, vy = "rear".equals(p.where) ? p.hy : -p.hy;   // heading points to the rear
            float big = Math.max(Math.abs(vx), Math.abs(vy));
            if (Math.abs(vx) >= 0.5f * big) { if (vx > 0) x0 += 0.5f * w; else x1 -= 0.5f * w; }
            if (Math.abs(vy) >= 0.5f * big) { if (vy > 0) y0 += 0.5f * h; else y1 -= 0.5f * h; }
        }
        return new float[]{x0, y0, x1, y1};
    }

    /** Detector thread, after every detection: pick this step's part and its place out of what was found, and box them. */
    private void chooseBoxes(final long seq, int n, String[] cls, float[] conf, float[][] box) {
        final Plan p = plan;
        if (!boxesLocal || p == null || !p.show) return;
        final int epoch = planEpoch;
        if (steadyEpoch != epoch) { steadyEpoch = epoch; pickSteady.reset(); placeSteady.reset(); }
        long now = SystemClock.elapsedRealtime();
        float[] pickBox = null, placeBox = null;
        if (p.pickId != null) {
            float floor = pickSteady.recent(now) ? p.keepConf : p.pickConf, best = -1;
            for (int i = 0; i < n; i++) if (cls[i].equals(p.pickCls) && conf[i] >= floor && conf[i] > best) { best = conf[i]; pickBox = box[i].clone(); }
        }
        if (p.placeId != null) {
            float floor = placeSteady.recent(now) ? p.keepConf : p.pickConf, bestArea = -1; float[] whole = null;
            for (int i = 0; i < n; i++) {
                if (conf[i] < floor) continue;
                boolean wanted = false;
                for (String c : p.placeClasses) if (c.equals(cls[i])) wanted = true;
                float area = (box[i][2] - box[i][0]) * (box[i][3] - box[i][1]);
                if (wanted && area > bestArea) { bestArea = area; whole = box[i]; }
            }
            if (whole != null) placeBox = subBox(whole, p);
        }
        int pickAct = p.pickId == null ? Steady.NONE : pickSteady.update(pickBox, now);
        int placeAct = p.placeId == null ? Steady.NONE : placeSteady.update(placeBox, now);
        int k = (pickAct != Steady.NONE ? 1 : 0) + (placeAct != Steady.NONE ? 1 : 0), o = 0;
        final String[] ids = new String[k], labels = new String[k], subs = new String[k], styles = new String[k];
        final float[][] boxes = new float[k][];
        if (pickAct != Steady.NONE) { ids[o] = p.pickId; labels[o] = p.pickLabel; subs[o] = ""; styles[o] = "pick"; boxes[o++] = pickAct == Steady.ANCHOR ? pickSteady.shown : null; }
        if (placeAct != Steady.NONE) { ids[o] = p.placeId; labels[o] = p.placeLabel; subs[o] = ""; styles[o] = "place"; boxes[o] = placeAct == Steady.ANCHOR ? placeSteady.shown : null; }
        cam.post(() -> {
            if (epoch != planEpoch) return;              // the step changed while this was on its way
            tracker.setGroup(GROUP_LOCAL, seq, ids, boxes, labels, subs, styles, SystemClock.elapsedRealtime());
            overlay.setTargets(tracker.snapshot(), tracker.awaySnapshot());
        });
        if (now - localLogAt >= 5000) {
            localLogAt = now;
            Log.i(TAG, "local boxes: pick " + (pickAct != Steady.NONE ? 1 : 0) + " place " + (placeAct != Steady.NONE ? 1 : 0) + " (step " + p.step + ")");
        }
    }

    /**
     * Turns the on-glasses detector on or off to match what is wanted now: the hub's config (or the test flag),
     * awake, in front, and not given up. Also notices that the hub is serving a newer model. UI thread.
     */
    private void updateDetector() {
        boolean want = (detectWanted || "glasses".equals(detectForced)) && !asleep && resumed && !detectFailed;
        Detector d = detector;
        if (want) {
            if (d == null) {
                d = new Detector(getFilesDir(), http, new Detector.Listener() {
                    @Override public void onDets(String json) {
                        WebSocket ws = socket;
                        if (ws != null && socketOpen) ws.send(json);
                    }
                    @Override public void onError(String text) {
                        Log.w(TAG, "detector off: " + text);
                        try {
                            WebSocket ws = socket;
                            if (ws != null && socketOpen) ws.send(new JSONObject().put("type", "dets_error").put("text", text).toString());
                        } catch (Exception ignored) { }
                        ui.post(() -> { detectFailed = true; lastDeviceKey = ""; sendDevice(); });
                    }
                    @Override public float trackerMs() { return tracker.msPerFrame(); }
                    @Override public void onResult(long seq, int n, String[] cls, float[] conf, float[][] box) { chooseBoxes(seq, n, cls, conf, box); }
                });
                detector = d;
            }
            if (!d.isRunning()) {
                Log.i(TAG, "detector on" + (detectWanted ? "" : " (forced by the launch flag)"));
                d.start(resolve(DETECT_META));
                lastDeviceKey = "";
                sendDevice();
            } else if (detectVersion > 0 && d.version() > 0 && detectVersion != d.version()) {
                d.refresh(resolve(DETECT_META));     // the hub has a newer model: fetched and swapped in while the old one keeps running
            }
        } else if (d != null && d.isRunning()) {
            Log.i(TAG, "detector off");
            d.stop();
            clearLocalBoxes();
            lastDeviceKey = "";
            sendDevice();
        }
    }

    @Override
    protected void onNewIntent(android.content.Intent intent) {
        super.onNewIntent(intent);
        if (intent != null && intent.getBooleanExtra("front", false)) return;   // our own "come back to the front": nothing to re-read
        setIntent(intent);
        handleIntent();
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        if (resumed) { cam.post(this::openCamera); startVoice(); }
    }

    private void startVoice() {
        if (asleep) return;
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) voice.start();
    }

    @Override
    protected void onResume() {
        super.onResume();
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                | View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_STABLE | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN);
        Log.i(TAG, "resumed");
        ui.removeCallbacks(comeToFront);
        resumed = true;
        brickView.setRunning(!asleep);
        if (voice != null) updateDetector();
        cam.post(this::openCamera);
        startVoice();
    }

    @Override
    protected void onPause() {
        Log.i(TAG, "paused" + (phoneMode ? "" : " (something is in front; camera, microphone and speech stay on)"));
        if (phoneMode) standDown();
        else ui.postDelayed(comeToFront, 2000);
        super.onPause();
    }

    @Override
    protected void onStop() {
        Log.i(TAG, "stopped");
        ui.removeCallbacks(comeToFront);
        if (!phoneMode) {
            standDown();
            // Fully covered by another screen while the lens is on and we are not asleep: take the front back.
            boolean lensOn = true;
            try { lensOn = ((android.os.PowerManager) getSystemService(Context.POWER_SERVICE)).isInteractive(); } catch (Exception ignored) { }
            if (lensOn && !asleep && !isFinishing()) ui.postDelayed(comeToFront, 2000);
        }
        super.onStop();
    }

    /**
     * The glasses are a one-app device: if a system dialog takes the front (it did when the hub restarted during
     * the demo, and the video, speech and touchpad all went dead), carry on, and take the front back.
     */
    private final Runnable comeToFront = () -> {
        if (isFinishing() || hasWindowFocus()) return;
        Log.i(TAG, "still behind something after 2 s: coming back to the front");
        try {
            startActivity(new android.content.Intent(this, MainActivity.class).putExtra("front", true)
                    .addFlags(android.content.Intent.FLAG_ACTIVITY_REORDER_TO_FRONT | android.content.Intent.FLAG_ACTIVITY_SINGLE_TOP));
        } catch (Exception e) { Log.w(TAG, "could not come to the front", e); }
    };

    /** Really in the background: release the camera and the microphone. */
    private void standDown() {
        resumed = false;
        updateDetector();
        brickView.setRunning(false);
        voice.stop();
        stopAudio();
        cam.removeCallbacksAndMessages(null);
        cam.post(this::closeCamera);
    }

    private void handleIntent() {
        SharedPreferences p = getSharedPreferences("h", MODE_PRIVATE);
        String given = getIntent().getStringExtra("url");
        if (given != null && !given.isEmpty()) p.edit().putString("url", given).apply();
        // Which camera: "-e camera 2" picks a phone's ultrawide (0.5x) lens, which is much
        // closer to the glasses' wide camera than the main lens. Saved like the url.
        Object cameraExtra = getIntent().getExtras() == null ? null : getIntent().getExtras().get("camera");
        if (cameraExtra != null) {
            try { p.edit().putInt("camera", Integer.parseInt(String.valueOf(cameraExtra))).apply(); }
            catch (NumberFormatException e) { Log.w(TAG, "bad camera extra " + cameraExtra); }
        }
        int wanted = p.getInt("camera", 0);
        // Zoom: "--ef zoom 0.5" asks a phone for its ultrawide lens (Camera2, zoom ratio below 1). Saved like the url.
        Object zoomExtra = getIntent().getExtras() == null ? null : getIntent().getExtras().get("zoom");
        if (zoomExtra != null) {
            try { p.edit().putFloat("zoom", Float.parseFloat(String.valueOf(zoomExtra))).apply(); }
            catch (NumberFormatException e) { Log.w(TAG, "bad zoom extra " + zoomExtra); }
        }
        float wantedZoom = p.getFloat("zoom", 1f);
        if (wanted != cameraId || wantedZoom != zoomWanted) {
            cameraId = wanted; zoomWanted = wantedZoom; cam2Failed = false;
            cam.post(() -> { closeCamera(); openCamera(); });
        }
        String u = p.getString("url", "ws://127.0.0.1:8000/ws");
        launchUrl = plain(u);
        if (hubUrls.isEmpty()) {                     // candidates from the last run, until this run's config replaces them
            try {
                JSONArray saved = new JSONArray(p.getString("hub_urls", "[]"));
                for (int i = 0; i < saved.length(); i++) hubUrls.add(plain(saved.getString(i)));
            } catch (Exception ignored) { }
        }
        url = withRole(launchUrl);
        socketOpen = false;
        linkSerial++;
        if (socket != null) socket.cancel();
        local("", url);
        connect();
    }

    // ---------- UI ----------

    private void buildUi() {
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);
        hud = new FrameLayout(this);

        // Instruction bar: [part picture] [step dots / instruction / hint], centred just above the bottom edge
        strip = new BarLayout(this);
        strip.setOrientation(LinearLayout.HORIZONTAL);
        strip.setGravity(Gravity.CENTER_VERTICAL);
        strip.setPadding(px(14), px(14), px(14), px(14));
        if (phoneMode) {                     // on the glasses black is transparent, so no background there
            GradientDrawable bg = new GradientDrawable();
            bg.setColor(Color.argb(140, 0, 0, 0));
            bg.setCornerRadius(18 * dp);
            strip.setBackground(bg);
        }

        leftSlot = new FrameLayout(this);
        brickView = new BrickView(this);
        brickView.setCompact(true);
        iconView = new ImageView(this);
        iconView.setScaleType(ImageView.ScaleType.FIT_CENTER);
        leftSlot.addView(brickView, new FrameLayout.LayoutParams(-1, -1));
        leftSlot.addView(iconView, new FrameLayout.LayoutParams(-1, -1));
        LinearLayout.LayoutParams leftLp = new LinearLayout.LayoutParams(px(52), px(52));
        leftLp.rightMargin = px(12);
        strip.addView(leftSlot, leftLp);

        LinearLayout mid = new LinearLayout(this);
        mid.setOrientation(LinearLayout.VERTICAL);
        dots = new DotsView(this);
        LinearLayout.LayoutParams dotsLp = new LinearLayout.LayoutParams(-2, -2);
        dotsLp.bottomMargin = px(6);
        mid.addView(dots, dotsLp);
        big = text(20, true, WHITE);
        big.setMaxLines(2);
        big.setEllipsize(TextUtils.TruncateAt.END);
        line2 = text(15, false, WHITE70);
        line2.setSingleLine(true);
        line2.setEllipsize(TextUtils.TruncateAt.END);
        LinearLayout.LayoutParams line2Lp = new LinearLayout.LayoutParams(-2, -2);
        line2Lp.topMargin = px(2);
        mid.addView(big, new LinearLayout.LayoutParams(-2, -2));
        mid.addView(line2, line2Lp);
        strip.addView(mid, new LinearLayout.LayoutParams(-2, -2));

        // Listening dot: sits in the bar's right padding, so it never changes the bar's size
        micDot = new View(this);
        GradientDrawable micShape = new GradientDrawable();
        micShape.setShape(GradientDrawable.OVAL);
        micShape.setColor(ACCENT);
        micDot.setBackground(micShape);
        micDot.setVisibility(View.INVISIBLE);

        FrameLayout.LayoutParams stripLp = new FrameLayout.LayoutParams(-2, -2, (BAR_AT_TOP ? Gravity.TOP : Gravity.BOTTOM) | Gravity.CENTER_HORIZONTAL);
        if (BAR_AT_TOP) stripLp.topMargin = px(14); else stripLp.bottomMargin = px(14);
        hud.addView(strip, stripLp);
        hud.addView(micDot, new FrameLayout.LayoutParams(px(6), px(6)));

        // Push-to-talk pill: a mic and "Listening…", just clear of the bar so it never covers the instruction
        pttPill = new LinearLayout(this);
        pttPill.setOrientation(LinearLayout.HORIZONTAL);
        pttPill.setGravity(Gravity.CENTER_VERTICAL);
        pttPill.setPadding(px(10), px(5), px(12), px(5));
        GradientDrawable pillBg = new GradientDrawable();
        pillBg.setColor(Color.BLACK);
        pillBg.setCornerRadius(100 * dp);
        pttPill.setBackground(pillBg);
        pttGlyph = new MicGlyph(this);
        LinearLayout.LayoutParams glyphLp = new LinearLayout.LayoutParams(px(12), px(16));
        glyphLp.rightMargin = px(6);
        pttPill.addView(pttGlyph, glyphLp);
        pttText = text(14, true, ACCENT);
        pttText.setSingleLine(true);
        pttPill.addView(pttText, new LinearLayout.LayoutParams(-2, -2));
        pttPill.setVisibility(View.GONE);
        hud.addView(pttPill, new FrameLayout.LayoutParams(-2, -2, (BAR_AT_TOP ? Gravity.TOP : Gravity.BOTTOM) | Gravity.CENTER_HORIZONTAL));
        strip.addOnLayoutChangeListener((v, l, t, r, bt, ol, ot, or, ob) -> { updateInsets(); placeMic(); placePtt(); });

        // Info block (reading a manual, all done, answers): centred text in the lower half
        info = new LinearLayout(this);
        info.setOrientation(LinearLayout.VERTICAL);
        info.setGravity(Gravity.CENTER_HORIZONTAL);
        info.setPadding(px(40), 0, px(40), 0);
        iBig = text(26, true, WHITE);
        iBig.setMaxLines(3);
        iBig.setEllipsize(TextUtils.TruncateAt.END);
        iBig.setGravity(Gravity.CENTER_HORIZONTAL);
        iSub = text(15, false, WHITE70);
        iSub.setMaxLines(2);
        iSub.setEllipsize(TextUtils.TruncateAt.MIDDLE);
        iSub.setGravity(Gravity.CENTER_HORIZONTAL);
        LinearLayout.LayoutParams iSubLp = new LinearLayout.LayoutParams(-2, -2);
        iSubLp.topMargin = px(4);
        info.addView(iBig, new LinearLayout.LayoutParams(-2, -2));
        info.addView(iSub, iSubLp);
        FrameLayout.LayoutParams infoLp = new FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM);
        infoLp.bottomMargin = px(30);
        hud.addView(info, infoLp);

        // Choice screen (which eye is dominant?): a question, the options as pills, a line of help
        choice = new LinearLayout(this);
        choice.setOrientation(LinearLayout.VERTICAL);
        choice.setGravity(Gravity.CENTER_HORIZONTAL);
        choice.setPadding(px(20), 0, px(20), 0);
        choice.setVisibility(View.GONE);
        cBig = text(22, true, WHITE);
        cBig.setGravity(Gravity.CENTER_HORIZONTAL);
        cBig.setMaxLines(2);
        cPills = new LinearLayout(this);
        cPills.setOrientation(LinearLayout.HORIZONTAL);
        cSub = text(14, false, WHITE70);
        cSub.setGravity(Gravity.CENTER_HORIZONTAL);
        cSub.setMaxLines(2);
        LinearLayout.LayoutParams pillsLp = new LinearLayout.LayoutParams(-2, -2);
        pillsLp.topMargin = px(20);
        LinearLayout.LayoutParams cSubLp = new LinearLayout.LayoutParams(-2, -2);
        cSubLp.topMargin = px(16);
        choice.addView(cBig, new LinearLayout.LayoutParams(-2, -2));
        choice.addView(cPills, pillsLp);
        choice.addView(cSub, cSubLp);
        FrameLayout.LayoutParams choiceLp = new FrameLayout.LayoutParams(-1, -2, Gravity.CENTER);
        choiceLp.bottomMargin = px(44);      // a little above the middle
        hud.addView(choice, choiceLp);

        // Peek card: the manual picture, shown briefly just above the bar, with rounded corners
        peek = new ImageView(this);
        peek.setAdjustViewBounds(true);
        peek.setMaxWidth(px(300));
        peek.setMaxHeight(px(190));
        peek.setScaleType(ImageView.ScaleType.FIT_CENTER);
        GradientDrawable peekShape = new GradientDrawable();
        peekShape.setColor(Color.BLACK);
        peekShape.setCornerRadius(14 * dp);
        peek.setBackground(peekShape);
        peek.setClipToOutline(true);
        peek.setAlpha(0f);
        peek.setVisibility(View.INVISIBLE);
        // the peek picture sits just clear of the bar: under it when the bar is at the top, above it otherwise
        FrameLayout.LayoutParams peekLp = new FrameLayout.LayoutParams(-2, -2, (BAR_AT_TOP ? Gravity.TOP : Gravity.BOTTOM) | Gravity.END);
        if (BAR_AT_TOP) peekLp.topMargin = px(128); else peekLp.bottomMargin = px(128);
        peekLp.rightMargin = px(12);
        hud.addView(peek, peekLp);

        // Step done: a ring that draws itself, then a tick
        flash = new DoneView(this);
        hud.addView(flash, new FrameLayout.LayoutParams(px(84), px(84), Gravity.CENTER));

        if (phoneMode) {
            // Simulated see-through: the live camera fills the screen behind the HUD.
            previewView = new TextureView(this);
            previewView.setSurfaceTextureListener(new TextureView.SurfaceTextureListener() {
                @Override public void onSurfaceTextureAvailable(SurfaceTexture st, int w, int h) {
                    previewSurface = st;
                    cam.post(() -> { closeCamera(); openCamera(); });
                }
                @Override public void onSurfaceTextureSizeChanged(SurfaceTexture st, int w, int h) { layoutPreview(); }
                @Override public boolean onSurfaceTextureDestroyed(final SurfaceTexture st) {
                    previewSurface = null;
                    cam.post(() -> { closeCamera(); st.release(); });
                    return false;
                }
                @Override public void onSurfaceTextureUpdated(SurfaceTexture st) { }
            });
            root.addView(previewView, new FrameLayout.LayoutParams(-1, -1));
        }
        root.addView(hud, new FrameLayout.LayoutParams(-1, -1));
        overlay = new ArOverlayView(this);
        overlay.setLead(phoneMode ? 0f : 0.06f);   // on a real lens the world is ahead of the camera picture
        root.addView(overlay, new FrameLayout.LayoutParams(-1, -1));

        // Sleep: a plain black sheet over everything; any touch on it wakes the glasses
        sleepCover = new View(this);
        sleepCover.setBackgroundColor(Color.BLACK);
        sleepCover.setVisibility(View.GONE);
        sleepCover.setOnTouchListener((v, e) -> {
            if (e.getAction() == MotionEvent.ACTION_DOWN) wakeByWearer();
            return true;                         // the whole touch is used up by waking: it is not also a tap
        });
        root.addView(sleepCover, new FrameLayout.LayoutParams(-1, -1));

        setContentView(root);
        root.setOnTouchListener((v, e) -> {
            if (e.getAction() == MotionEvent.ACTION_UP) tap();
            return true;
        });
    }

    private int px(float dps) { return Math.round(dps * dp); }

    /** Phone mode: show the whole camera frame, undistorted, centred; tell the AR layer where it is. */
    private void layoutPreview() {
        if (previewView == null || camW == 0 || camH == 0) return;
        float vw = previewView.getWidth(), vh = previewView.getHeight();
        if (vw <= 0 || vh <= 0) return;
        float sc = Math.min(vw / camW, vh / camH), dw = camW * sc, dh = camH * sc;
        Matrix mx = new Matrix();
        int rot = getWindowManager().getDefaultDisplay().getRotation();
        if (cam2 != null && (rot == Surface.ROTATION_90 || rot == Surface.ROTATION_270)) {
            // Camera2 hands the texture a picture turned for a portrait screen. Undo the stretch, turn it
            // for this landscape screen, then scale it to the same centred rectangle the old API fills.
            RectF view = new RectF(0, 0, vw, vh), buf = new RectF(0, 0, camH, camW);
            buf.offset(view.centerX() - buf.centerX(), view.centerY() - buf.centerY());
            mx.setRectToRect(view, buf, Matrix.ScaleToFit.FILL);
            mx.postScale(sc, sc, view.centerX(), view.centerY());
            mx.postRotate(90 * (rot - 2), view.centerX(), view.centerY());
        } else {
            mx.setScale(dw / vw, dh / vh, vw / 2f, vh / 2f);
        }
        previewView.setTransform(mx);
        overlay.setPreviewRect(new RectF((vw - dw) / 2f, (vh - dh) / 2f, (vw + dw) / 2f, (vh + dh) / 2f));
    }

    // ---------- Push-to-talk ----------

    private static final int PTT_OFF = 0, PTT_LISTENING = 1, PTT_THINKING = 2;
    private int pttState = PTT_OFF;
    private LinearLayout pttPill;
    private MicGlyph pttGlyph;
    private TextView pttText;
    private android.animation.ObjectAnimator pttPulse;
    private ToneGenerator softTones;
    private final Runnable pttTimeout = () -> { if (pttState == PTT_THINKING) setPtt(PTT_OFF); };

    /** A tiny microphone drawn with strokes, in the colour of the text beside it. */
    private static class MicGlyph extends View {
        private final Paint p = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final RectF r = new RectF();
        MicGlyph(Context c) {
            super(c);
            p.setStyle(Paint.Style.STROKE);
            p.setStrokeCap(Paint.Cap.ROUND);
            p.setStrokeWidth(1.6f * c.getResources().getDisplayMetrics().density);
            p.setColor(ACCENT);
        }
        void setColor(int color) { p.setColor(color); invalidate(); }
        @Override protected void onDraw(Canvas c) {
            float w = getWidth(), h = getHeight(), s = p.getStrokeWidth();
            r.set(w * 0.30f, s, w * 0.70f, h * 0.58f);
            c.drawRoundRect(r, w * 0.2f, w * 0.2f, p);                      // the capsule
            r.set(s, h * 0.10f, w - s, h * 0.76f);
            c.drawArc(r, 0, 180, false, p);                                 // the cradle
            c.drawLine(w / 2, h * 0.76f, w / 2, h - s, p);                  // the stem
        }
    }

    /** A tap on the touchpad: start listening, or cancel if already listening. */
    private void togglePtt() {
        if (pttState == PTT_LISTENING) { voice.pttCancel(); return; }
        if (!voice.isRunning()) { Log.w(TAG, "ptt: microphone is not running"); return; }
        stopAudio();                             // stop talking so we can listen
        try {
            if (softTones == null) softTones = new ToneGenerator(AudioManager.STREAM_MUSIC, 30);
            softTones.startTone(ToneGenerator.TONE_PROP_BEEP, 60);         // a short soft blip
        } catch (Exception e) { Log.w(TAG, "no blip", e); }
        voice.pttStart();
        send("ptt");
        setPtt(PTT_LISTENING);
    }

    private void setPtt(int state) {
        pttState = state;
        ui.removeCallbacks(pttTimeout);
        if (pttPulse != null) { pttPulse.cancel(); pttPulse = null; }
        pttPill.setAlpha(1f);
        if (state == PTT_OFF) { pttPill.setVisibility(View.GONE); return; }
        boolean listening = state == PTT_LISTENING;
        int color = listening ? ACCENT : WHITE70;
        pttText.setText(listening ? "Listening…" : "Thinking…");
        pttText.setTextColor(color);
        pttGlyph.setColor(color);
        ((GradientDrawable) pttPill.getBackground()).setStroke(Math.max(1, Math.round(dp)), listening ? Color.argb(150, 0x39, 0xFF, 0x14) : Color.argb(60, 255, 255, 255));
        placePtt();
        pttPill.setVisibility(View.VISIBLE);
        if (listening) {
            pttPulse = android.animation.ObjectAnimator.ofFloat(pttPill, "alpha", 1f, 0.6f);
            pttPulse.setDuration(400);           // 100% to 60% and back in 800 ms
            pttPulse.setRepeatMode(android.animation.ValueAnimator.REVERSE);
            pttPulse.setRepeatCount(android.animation.ValueAnimator.INFINITE);
            pttPulse.start();
        } else {
            ui.postDelayed(pttTimeout, 4000);     // no answer: stop saying "Thinking…"
        }
    }

    /** Just clear of the instruction bar; near the edge when there is no bar. */
    private void placePtt() {
        if (pttPill == null) return;
        boolean bar = strip.getVisibility() == View.VISIBLE && strip.getHeight() > 0 && hud.getHeight() > 0;
        FrameLayout.LayoutParams lp = (FrameLayout.LayoutParams) pttPill.getLayoutParams();
        int gap = px(8);
        int top = bar ? strip.getBottom() + gap : px(14), bottom = bar ? hud.getHeight() - strip.getTop() + gap : px(14);
        if (BAR_AT_TOP ? lp.topMargin == top : lp.bottomMargin == bottom) return;
        if (BAR_AT_TOP) lp.topMargin = top; else lp.bottomMargin = bottom;
        pttPill.setLayoutParams(lp);
    }

    private static final int MIC_IDLE = 0, MIC_CAPTURING = 1, MIC_WAITING = 2;
    private int micState = MIC_IDLE;
    private android.animation.ObjectAnimator micPulse;
    private final Runnable micTimeout = () -> { if (micState == MIC_WAITING) setMic(MIC_IDLE); };

    /** Listening dot: pulsing while an utterance is being captured, steady at half strength while the hub answers. */
    private void setMic(int state) {
        micState = state;
        ui.removeCallbacks(micTimeout);
        if (micPulse != null) { micPulse.cancel(); micPulse = null; }
        if (state == MIC_CAPTURING) {
            micPulse = android.animation.ObjectAnimator.ofFloat(micDot, "alpha", 1f, 0.3f);
            micPulse.setDuration(450);
            micPulse.setRepeatMode(android.animation.ValueAnimator.REVERSE);
            micPulse.setRepeatCount(android.animation.ValueAnimator.INFINITE);
            micPulse.start();
        } else if (state == MIC_WAITING) {
            micDot.setAlpha(0.5f);
            ui.postDelayed(micTimeout, 6000);     // no answer: stop waiting
        }
        placeMic();
    }

    private void placeMic() {
        boolean show = micState != MIC_IDLE && strip.getVisibility() == View.VISIBLE && strip.getWidth() > 0;
        micDot.setVisibility(show ? View.VISIBLE : View.INVISIBLE);
        if (!show) return;
        micDot.setX(strip.getRight() - px(10) - px(3));
        micDot.setY(strip.getTop() + strip.getHeight() / 2f - px(3));
    }

    /** Keep AR boxes and labels clear of the instruction bar (and of the glasses' status bar at the top). */
    private void updateInsets() {
        if (overlay == null) return;
        boolean bar = strip.getVisibility() == View.VISIBLE && strip.getHeight() > 0 && hud.getHeight() > 0;
        float statusBar = phoneMode ? 0 : 28 * dp;
        if (BAR_AT_TOP) overlay.setInsets(bar ? Math.max(statusBar, strip.getBottom() + 6 * dp) : statusBar, 0);
        else overlay.setInsets(statusBar, bar ? hud.getHeight() - strip.getTop() + 6 * dp : 0);
    }

    private void setCalibMode(boolean on) {
        if (phoneMode) return;               // the phone preview needs no calibration
        calibMode = on;
        hud.setVisibility(on ? View.INVISIBLE : View.VISIBLE);
        overlay.setCalibMode(on);
    }

    private TextView text(int sp, boolean bold, int color) {
        TextView t = new TextView(this);
        if (phoneMode) t.setShadowLayer(4 * dp, 0, 0, Color.BLACK);
        t.setTextSize(sp);
        t.setTextColor(color);
        t.setTypeface(bold ? SEMIBOLD : Typeface.DEFAULT);
        t.setIncludeFontPadding(false);
        return t;
    }

    private static final Typeface SEMIBOLD = Typeface.create(Typeface.DEFAULT, 600, false);

    /** The instruction bar: as wide as its content, between 46% and 88% of the screen. */
    private static class BarLayout extends LinearLayout {
        BarLayout(Context c) { super(c); }
        @Override protected void onMeasure(int wSpec, int hSpec) {
            int parent = MeasureSpec.getSize(wSpec);
            setMinimumWidth(Math.round(parent * 0.46f));
            super.onMeasure(MeasureSpec.makeMeasureSpec(Math.round(parent * 0.88f), MeasureSpec.AT_MOST), hSpec);
        }
    }

    /** Step dots: finished = accent, current = white and a little bigger, upcoming = faint white. */
    private static class DotsView extends View {
        private static final int MAX = 12;       // longer guides show a window of dots around the current step
        private final Paint p = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final float dp;
        private int step, total;
        DotsView(Context c) { super(c); dp = c.getResources().getDisplayMetrics().density; }
        void set(int step, int total) {
            if (step == this.step && total == this.total) return;
            this.step = step; this.total = total;
            setVisibility(total > 0 ? VISIBLE : GONE);
            requestLayout(); invalidate();
        }
        @Override protected void onMeasure(int w, int h) {
            int n = Math.min(total, MAX);
            setMeasuredDimension(n <= 0 ? 0 : Math.round((n * 6 + (n - 1) * 6 + 2) * dp), Math.round(8 * dp));
        }
        @Override protected void onDraw(Canvas c) {
            int n = Math.min(total, MAX);
            if (n <= 0) return;
            int first = total <= MAX ? 1 : Math.max(1, Math.min(total - MAX + 1, step - MAX / 2));
            float x = 4 * dp, cy = getHeight() / 2f;
            for (int i = 0; i < n; i++, x += 12 * dp) {
                int k = first + i;
                p.setColor(k < step ? ACCENT : k == step ? WHITE : Color.argb(71, 255, 255, 255));
                c.drawCircle(x, cy, (k == step ? 4 : 3) * dp, p);
            }
        }
    }

    /**
     * Step done: a ring draws around in 300 ms, a tick draws in 200 ms, it holds 250 ms and fades in 200 ms.
     * Wrong part: the same in red, with an X (two strokes of 100 ms each) in place of the tick.
     */
    private static class DoneView extends View {
        private final Paint p = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Path tick = new Path(), part = new Path();
        private final android.graphics.PathMeasure measure = new android.graphics.PathMeasure();
        private final RectF ring = new RectF();
        private final float dp;
        private long startedAt;
        private boolean wrong;
        DoneView(Context c) {
            super(c);
            dp = c.getResources().getDisplayMetrics().density;
            p.setStyle(Paint.Style.STROKE);
            p.setStrokeWidth(3 * dp);
            p.setStrokeCap(Paint.Cap.ROUND);
            p.setStrokeJoin(Paint.Join.ROUND);
            p.setColor(ACCENT);
        }
        void play(boolean wrong) { this.wrong = wrong; startedAt = SystemClock.uptimeMillis(); invalidate(); }
        @Override protected void onDraw(Canvas c) {
            if (startedAt == 0) return;
            long t = SystemClock.uptimeMillis() - startedAt;
            if (t >= 950) { startedAt = 0; return; }
            float cx = getWidth() / 2f, cy = getHeight() / 2f, r = 36 * dp;
            p.setColor(wrong ? ArOverlayView.RED : ACCENT);
            p.setAlpha(t < 750 ? 255 : Math.round(255 * (950 - t) / 200f));
            ring.set(cx - r, cy - r, cx + r, cy + r);
            c.drawArc(ring, -90, 360f * Math.min(1f, t / 300f), false, p);
            if (t > 300 && wrong) {
                float k = r * 0.34f, a = Math.min(1f, (t - 300) / 100f), b = Math.min(1f, (t - 400) / 100f);
                c.drawLine(cx - k, cy - k, cx - k + 2 * k * a, cy - k + 2 * k * a, p);
                if (b > 0) c.drawLine(cx + k, cy - k, cx + k - 2 * k * b, cy - k + 2 * k * b, p);
            } else if (t > 300) {
                tick.rewind();
                tick.moveTo(cx - r * 0.42f, cy + r * 0.04f);
                tick.lineTo(cx - r * 0.10f, cy + r * 0.36f);
                tick.lineTo(cx + r * 0.46f, cy - r * 0.30f);
                measure.setPath(tick, false);
                part.rewind();
                measure.getSegment(0, measure.getLength() * Math.min(1f, (t - 300) / 200f), part, true);
                c.drawPath(part, p);
            }
            postInvalidateOnAnimation();
        }
    }

    private static String str(JSONObject m, String key) {
        return m.isNull(key) ? "" : m.optString(key, "");
    }

    /** The app's own waiting screen: a calm "Connecting…" with the address small underneath. */
    private LinearLayout choice, cPills;
    private TextView cBig, cSub;
    private volatile boolean choiceMode;     // a choice screen is showing: keys move and confirm, no AR
    private String shownOptions = "";

    /** {"mode":"choice","big","sub","options":[...],"selected":i}: the hub re-sends it after every move. */
    private void renderChoice(JSONObject m) {
        JSONArray opts = m.optJSONArray("options");
        int n = opts == null ? 0 : Math.min(4, opts.length()), selected = m.optInt("selected", 0);
        cBig.setText(str(m, "big"));
        String s = str(m, "sub");
        cSub.setText(s);
        cSub.setVisibility(s.isEmpty() ? View.GONE : View.VISIBLE);
        String key = String.valueOf(opts);
        if (!key.equals(shownOptions)) {         // new set of options: build the pills
            shownOptions = key;
            cPills.removeAllViews();
            int gap = px(16), room = getResources().getDisplayMetrics().widthPixels - px(40);
            int w = n == 0 ? 0 : Math.min(px(190), (room - gap * (n - 1)) / n);
            for (int i = 0; i < n; i++) {
                TextView pill = text(18, true, WHITE70);
                pill.setGravity(Gravity.CENTER);
                pill.setSingleLine(true);
                pill.setEllipsize(TextUtils.TruncateAt.END);
                pill.setText(opts.optString(i));
                pill.setBackground(new GradientDrawable());
                LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(w, px(60));
                if (i > 0) lp.leftMargin = gap;
                cPills.addView(pill, lp);
            }
        }
        for (int i = 0; i < cPills.getChildCount(); i++) {
            TextView pill = (TextView) cPills.getChildAt(i);
            GradientDrawable bg = (GradientDrawable) pill.getBackground();
            bg.setCornerRadius(30 * dp);
            if (i == selected) {
                bg.setColor(ACCENT);
                bg.setStroke(0, 0);
                pill.setTextColor(Color.BLACK);
            } else {
                bg.setColor(Color.TRANSPARENT);
                bg.setStroke(Math.max(1, Math.round(1.5f * dp)), Color.argb(77, 255, 255, 255));
                pill.setTextColor(WHITE70);
            }
        }
    }

    private void local(String why, String address) {
        try {
            if (why != null && !why.isEmpty()) Log.i(TAG, "waiting for the hub: " + why);
            renderScreen(new JSONObject().put("mode", "info").put("local", true).put("big", "Connecting…").put("sub", address));
        } catch (Exception e) { Log.w(TAG, "local screen", e); }
    }

    private static final java.util.regex.Pattern STEP_OF = java.util.regex.Pattern.compile("(\\d+)\\s*/\\s*(\\d+)");

    private void renderScreen(JSONObject m) {
        boolean infoMode = "info".equals(m.optString("mode"));
        boolean choosing = "choice".equals(m.optString("mode"));
        boolean local = m.optBoolean("local");
        boolean warn = m.optBoolean("warn");
        String t = str(m, "top"), bg = str(m, "big"), s = str(m, "sub");
        hintActive = false;

        hidePeek(false);
        currentImage = resolve(str(m, "image"));
        if (!currentImage.isEmpty() && images.get(currentImage) == null) loadBitmap(currentImage);   // prefetch

        if (choosing != choiceMode) {
            choiceMode = choosing;
            choice.setVisibility(choosing ? View.VISIBLE : View.GONE);
            overlay.setVisibility(choosing ? View.INVISIBLE : View.VISIBLE);   // no AR boxes or arrows while choosing
            if (choosing) {
                ui.removeCallbacks(singleTap);
                lastTapAt = 0;
                cam.post(() -> {                 // forget what was being tracked, so nothing old pops back afterwards
                    tracker.setTargets(frameSeq, new String[0], new float[0][], new String[0], new String[0], new String[0], SystemClock.elapsedRealtime());
                    overlay.setTargets(tracker.snapshot(), tracker.awaySnapshot());
                });
            }
        }
        if (choosing) {
            strip.setVisibility(View.GONE);
            info.setVisibility(View.GONE);
            brickView.setBricks(null);
            currentIcon = "";
            renderChoice(m);
        } else if (infoMode) {
            strip.setVisibility(View.GONE);
            info.setVisibility(View.VISIBLE);
            brickView.setBricks(null);
            currentIcon = "";
            // the waiting screen sits in the middle and is quieter than a real message
            ((FrameLayout.LayoutParams) info.getLayoutParams()).gravity = local ? Gravity.CENTER : Gravity.BOTTOM;
            info.requestLayout();
            iBig.setText(bg);
            iBig.setTextSize(local ? 18 : 26);
            iBig.setTypeface(local ? Typeface.DEFAULT : SEMIBOLD);
            iBig.setTextColor(local ? WHITE70 : warn ? WARN : WHITE);
            iSub.setText(s);
            iSub.setTextSize(local ? 12 : 15);
            iSub.setTextColor(local ? WHITE40 : WHITE70);
            iSub.setVisibility(s.isEmpty() ? View.GONE : View.VISIBLE);
        } else {
            info.setVisibility(View.GONE);
            strip.setVisibility(View.VISIBLE);
            big.setText(bg);
            big.setTextColor(warn ? WARN : WHITE);
            int step = m.optInt("step", 0), total = m.optInt("total", 0);
            if (total <= 0) {                      // older hubs only send "2/5" in top
                java.util.regex.Matcher mt = STEP_OF.matcher(t);
                if (mt.find()) { step = Integer.parseInt(mt.group(1)); total = Integer.parseInt(mt.group(2)); }
            }
            dots.set(step, total);
            subText = s;
            applyLine2();
            // part picture: the hub's icon if there is one, else the first brick drawn in 3D, else nothing
            currentIcon = resolve(str(m, "icon"));
            int n = brickView.setBricks(currentIcon.isEmpty() ? m.optJSONArray("bricks") : null);
            brickView.setVisibility(n > 0 ? View.VISIBLE : View.GONE);
            if (currentIcon.isEmpty()) {
                iconView.setVisibility(View.GONE);
                iconView.setImageBitmap(null);
            } else {
                iconView.setVisibility(View.VISIBLE);
                Bitmap cached = images.get(currentIcon);
                iconView.setImageBitmap(cached);
                if (cached == null) loadBitmap(currentIcon);
            }
            leftSlot.setVisibility(n > 0 || !currentIcon.isEmpty() ? View.VISIBLE : View.GONE);
        }
        updateInsets();
        placeMic();
        if (pttState == PTT_THINKING && !local) setPtt(PTT_OFF);
        placePtt();

        int peekMs = m.optInt("peek_ms", 0);
        if (peekMs > 0) showPeek(peekMs);
        boolean done = "done".equals(m.optString("flash")), wrong = "wrong".equals(m.optString("flash"));
        if (done || wrong) flash.play(wrong);
        String sound = m.optString("sound");
        beep(done ? "done" : wrong ? "warn" : sound);
        say(str(m, "say_url"));
    }

    private void renderHint(JSONObject m) {
        String text = str(m, "text");
        hintActive = !text.isEmpty();
        hintText = text;
        hintTone = m.optString("tone");
        if (pttState == PTT_THINKING) setPtt(PTT_OFF);
        if (micState == MIC_WAITING) setMic(MIC_IDLE);
        applyLine2();
        int peekMs = m.optInt("peek_ms", 0);
        if (peekMs > 0) showPeek(peekMs);
        say(str(m, "say_url"));
    }

    /** Second line of the bar: the live hint while one is active, else the step's sub text; gone when both are empty. */
    private void applyLine2() {
        if (hintActive) {
            line2.setText(hintText);
            line2.setTextColor("warn".equals(hintTone) ? WARN : "ok".equals(hintTone) ? ACCENT : WHITE70);
            line2.setVisibility(View.VISIBLE);
        } else {
            line2.setText(subText);
            line2.setTextColor(WHITE70);
            line2.setVisibility(subText.isEmpty() ? View.GONE : View.VISIBLE);
        }
    }

    // ---------- Peek card ----------

    private final Runnable hidePeekTask = () -> hidePeek(true);

    private void showPeek(int ms) {
        if (currentImage.isEmpty()) return;
        peekUntil = SystemClock.uptimeMillis() + ms;
        Bitmap b = images.get(currentImage);
        if (b == null) { loadBitmap(currentImage); return; }   // shown by onBitmap when it arrives
        displayPeek(b, ms);
    }

    private void displayPeek(Bitmap b, long ms) {
        ui.removeCallbacks(hidePeekTask);
        peek.animate().cancel();
        peek.setImageBitmap(b);
        peek.setVisibility(View.VISIBLE);
        peek.animate().alpha(1f).setStartDelay(0).setDuration(150).start();
        ui.postDelayed(hidePeekTask, ms);
    }

    private void hidePeek(boolean fade) {
        ui.removeCallbacks(hidePeekTask);
        peekUntil = 0;
        peek.animate().cancel();
        if (fade) {
            peek.animate().alpha(0f).setStartDelay(0).setDuration(250).withEndAction(() -> peek.setVisibility(View.INVISIBLE)).start();
        } else {
            peek.setAlpha(0f);
            peek.setVisibility(View.INVISIBLE);
        }
    }

    // ---------- Images ----------

    /** Paths are relative to the backend's HTTP origin (same host:port as the WebSocket). */
    private String resolve(String path) {
        if (path == null || path.isEmpty() || "null".equals(path)) return "";
        if (path.startsWith("http://") || path.startsWith("https://")) return path;
        String base = url == null ? "" : url.replaceFirst("^ws", "http");
        int scheme = base.indexOf("://");
        int slash = base.indexOf('/', scheme < 0 ? 0 : scheme + 3);
        if (slash > 0) base = base.substring(0, slash);
        int q = base.indexOf('?');
        if (q > 0) base = base.substring(0, q);
        return base + (path.startsWith("/") ? path : "/" + path);
    }

    private void loadBitmap(final String full) {
        if (!loading.add(full)) return;
        try {
            http.newCall(new Request.Builder().url(full).build()).enqueue(new Callback() {
                @Override public void onFailure(Call call, java.io.IOException e) {
                    Log.w(TAG, "image failed " + full + " " + e);
                    ui.post(() -> loading.remove(full));
                }
                @Override public void onResponse(Call call, Response r) {
                    Bitmap bmp = null;
                    try {
                        if (r.isSuccessful() && r.body() != null) {
                            byte[] bytes = r.body().bytes();
                            bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.length);
                        }
                        if (bmp == null) Log.w(TAG, "image " + r.code() + " unusable " + full);
                    } catch (Exception e) { Log.w(TAG, "image failed " + full, e);
                    } finally { r.close(); }
                    final Bitmap got = bmp;
                    ui.post(() -> { loading.remove(full); if (got != null) onBitmap(full, got); });
                }
            });
        } catch (Exception e) {
            loading.remove(full);
            Log.w(TAG, "bad image url " + full, e);
        }
    }

    private void onBitmap(String full, Bitmap bmp) {
        images.put(full, bmp);
        if (full.equals(currentIcon)) iconView.setImageBitmap(bmp);
        long left = peekUntil - SystemClock.uptimeMillis();
        if (full.equals(currentImage) && left > 200) displayPeek(bmp, left);
    }

    // ---------- Audio ----------

    private void beep(String sound) {
        if (asleep || tones == null || sound == null || sound.isEmpty()) return;
        try {
            if ("done".equals(sound)) { tones.startTone(ToneGenerator.TONE_PROP_ACK, 180); quietMic(180 + 400); }
            else if ("warn".equals(sound)) { tones.startTone(ToneGenerator.TONE_PROP_NACK, 350); quietMic(350 + 400); }
        } catch (Exception e) { Log.w(TAG, "tone failed", e); }
    }

    /** Download the clip, then play it. A newer clip replaces one that is still playing. */
    private void say(String path) {
        final String full = resolve(path);
        if (full.isEmpty() || asleep) return;
        stopAudio();
        final int token = ++sayToken;
        speaking = true;
        try {
            http.newCall(new Request.Builder().url(full).build()).enqueue(new Callback() {
                @Override public void onFailure(Call call, java.io.IOException e) { ui.post(() -> sayFailed(token, "download failed: " + e)); }
                @Override public void onResponse(Call call, Response r) {
                    File f = null;
                    try {
                        if (r.isSuccessful() && r.body() != null) {
                            f = new File(getCacheDir(), "say-" + (token % 4) + ".mp3");
                            try (FileOutputStream out = new FileOutputStream(f)) { out.write(r.body().bytes()); }
                        }
                    } catch (Exception e) { f = null;
                    } finally { r.close(); }
                    final File clip = f;
                    final int code = r.code();
                    ui.post(() -> { if (clip == null) sayFailed(token, "download failed: hub answered " + code); else play(clip, token); });
                }
            });
        } catch (Exception e) { sayFailed(token, "download failed: " + e); }
    }

    private void quietMic(long ms) {
        muteUntil = Math.max(muteUntil, SystemClock.uptimeMillis() + ms);
    }

    private void sayFailed(int token, String why) {
        if (why.startsWith("replaced")) Log.i(TAG, "say: clip skipped (" + why + ")");
        else Log.w(TAG, "say: clip not played (" + why + ")");
        if (token == sayToken) { speaking = false; speakEndAt = SystemClock.uptimeMillis(); quietMic(400); }
    }

    private void play(File clip, int token) {
        if (token != sayToken) { sayFailed(token, "replaced by a newer clip, or by a tap to talk"); return; }
        if (!resumed) { sayFailed(token, "the app is in the background"); return; }
        if (asleep) { sayFailed(token, "asleep"); return; }
        try {
            final MediaPlayer mp = new MediaPlayer();
            player = mp;
            mp.setAudioStreamType(AudioManager.STREAM_MUSIC);
            mp.setDataSource(clip.getAbsolutePath());
            mp.setOnPreparedListener(p -> { Log.i(TAG, "say start " + p.getDuration() + " ms"); p.start(); });
            mp.setOnCompletionListener(p -> { Log.i(TAG, "say done"); endAudio(p); });
            mp.setOnErrorListener((p, a, b) -> { Log.w(TAG, "say error " + a + " " + b); endAudio(p); return true; });
            mp.prepareAsync();
        } catch (Exception e) {
            Log.w(TAG, "say failed", e);
            if (player != null) endAudio(player);
            sayFailed(token, "player failed: " + e);
        }
    }

    private void endAudio(MediaPlayer p) {
        try { p.release(); } catch (Exception ignored) { }
        if (player == p) { player = null; speaking = false; speakEndAt = SystemClock.uptimeMillis(); quietMic(400); }
    }

    private void stopAudio() {
        sayToken++;
        MediaPlayer p = player;
        player = null;
        if (p != null) {
            try { p.stop(); } catch (Exception ignored) { }
            try { p.release(); } catch (Exception ignored) { }
            quietMic(400);
        }
        speaking = false;
        speakEndAt = SystemClock.uptimeMillis();
    }

    // ---------- WebSocket ----------

    // ---------- Hub link: USB (adb reverse to 127.0.0.1) or Wi-Fi, whichever answers ----------

    private String launchUrl = "ws://127.0.0.1:8000/ws";
    private final ArrayList<String> hubUrls = new ArrayList<>();   // the hub's own list of ways to reach it, loopback first
    private boolean hubUrlsFresh;                // received in this run (before that the launch URL goes first)
    private volatile int linkSerial;             // bumps whenever the current connection attempt is given up
    private int linkFails;
    private volatile long lastRxAt;              // elapsedRealtime of the last message from the hub
    private volatile boolean hubPings;           // this hub sends {"type":"ping"} every 1.5 s, so silence means a dead link
    // Short connect timeout so a dead candidate costs 1.5 s; WebSocket pings so a silently dead link is noticed.
    // No message compression: OkHttp 4.12 crashes the whole app (ArrayIndexOutOfBounds in MessageDeflater) when a
    // compressed write is cut off mid-frame, which is exactly what happens when Wi-Fi drops. Our frames are JPEG anyway.
    // Two clients: the USB tunnel answers at once or not at all; Wi-Fi at the venue needs patience (ping 80-600 ms, bursts of loss).
    private final OkHttpClient usbHttp = http.newBuilder()
            .connectTimeout(1000, java.util.concurrent.TimeUnit.MILLISECONDS)
            .pingInterval(2000, java.util.concurrent.TimeUnit.MILLISECONDS)
            .addNetworkInterceptor(chain -> chain.proceed(chain.request().newBuilder().removeHeader("Sec-WebSocket-Extensions").build()))
            .build();
    private final OkHttpClient wifiHttp = usbHttp.newBuilder()
            .connectTimeout(3000, java.util.concurrent.TimeUnit.MILLISECONDS)
            .pingInterval(6000, java.util.concurrent.TimeUnit.MILLISECONDS)      // fails only when a pong takes more than 6 s
            .build();
    private volatile boolean usbCable = true;    // the USB cable is in (from the battery broadcast); loopback is only tried then
    private volatile boolean linkIsWifi;         // the open link goes over Wi-Fi: upload gently
    private long linkDownSince;                  // elapsedRealtime when the link went away; 0 while it is up
    private boolean waitingShown;
    private final Runnable showWaiting = () -> {  // only after 5 s without a link: short drops leave the lens as it is
        if (socketOpen || !alive) return;
        waitingShown = true;
        local("no hub link for 5 s", plain(url));
    };

    private static String plain(String u) { int q = u.indexOf('?'); return q < 0 ? u : u.substring(0, q); }
    private static String withRole(String u) { return u.contains("?") ? u : u + "?role=glasses"; }
    private static boolean isUsb(String u) { return u.contains("://127.0.0.1") || u.contains("://localhost"); }

    /** Every way to reach the hub, in the order to try them. The USB tunnel (127.0.0.1) only counts while the cable is in. */
    private ArrayList<String> candidates() {
        ArrayList<String> all = new ArrayList<>();
        if (!hubUrlsFresh) all.add(launchUrl);
        for (String u : hubUrls) if (!all.contains(u)) all.add(u);
        if (all.isEmpty()) all.add(launchUrl);
        ArrayList<String> c = new ArrayList<>();
        for (String u : all) if (isUsb(u) && usbCable) c.add(u);          // cable in: the tunnel first
        for (String u : all) if (!isUsb(u)) c.add(u);
        return c.isEmpty() ? all : c;                                       // nothing but the tunnel known: try it anyway
    }

    private void connect() {
        alive = true;
        final int serial = ++linkSerial;
        final String target = url;
        final boolean overWifi = !isUsb(target);
        final boolean[] opened = {false};
        Request req = new Request.Builder().url(target).build();
        socket = (overWifi ? wifiHttp : usbHttp).newWebSocket(req, new WebSocketListener() {
            @Override public void onOpen(WebSocket ws, Response r) {
                if (serial != linkSerial) { ws.cancel(); return; }
                opened[0] = true;
                socketOpen = true;
                linkIsWifi = overWifi;
                lastRxAt = SystemClock.elapsedRealtime();
                hubPings = false;
                Log.i(TAG, "hub link: " + plain(target));
                lastCameraRestartAt = SystemClock.elapsedRealtime();   // a fresh connection gets 5 s before the watchdog acts
                ui.post(() -> {
                    linkFails = 0;
                    linkDownSince = 0;
                    ui.removeCallbacks(showWaiting);
                    waitingShown = false;
                    configSinceConnect = false; lastDeviceKey = "";      // the hello that follows sends the device message
                    detectFailed = false;                                 // a fresh connection: the model is worth another look
                    Detector d = detector;
                    if (d != null && d.isRunning()) d.refresh(resolve(DETECT_META));
                });
                cam.post(() -> sendHello());
            }
            @Override public void onMessage(WebSocket ws, String text) {
                lastRxAt = SystemClock.elapsedRealtime();
                try {
                    final JSONObject m = new JSONObject(text);
                    String type = m.optString("type");
                    if ("screen".equals(type)) ui.post(() -> renderScreen(m));
                    else if ("hint".equals(type)) ui.post(() -> renderHint(m));
                    else if ("config".equals(type)) applyConfig(m);
                    else if ("ar".equals(type)) applyAr(m);
                    else if ("ping".equals(type)) hubPings = true;
                    else if ("plan".equals(type)) applyPlan(m);
                    else if ("wifi".equals(type)) applyWifi(m);
                    else if ("calib".equals(type)) ui.post(() -> setCalibMode(m.optBoolean("on")));
                    // every other message type (status, ...) is ignored on purpose
                } catch (Exception e) { Log.w(TAG, "bad message: " + e.getClass().getSimpleName()); }   // never the text: it may hold a Wi-Fi password
            }
            @Override public void onFailure(WebSocket ws, Throwable t, Response r) {
                if (serial != linkSerial) return;
                socketOpen = false;
                if (opened[0]) Log.w(TAG, "hub link lost (" + plain(target) + "): " + t);
                else Log.w(TAG, "connect failed " + plain(target) + ": " + t);
                nextLink(serial);
            }
            @Override public void onClosing(WebSocket ws, int code, String reason) { ws.close(1000, null); }
            @Override public void onClosed(WebSocket ws, int code, String reason) {
                if (serial != linkSerial) return;
                socketOpen = false;
                Log.w(TAG, "hub link closed (" + plain(target) + ")");
                nextLink(serial);
            }
        });
    }

    /**
     * The current link failed: move to the next candidate and try again, for ever. The first retry is immediate,
     * later ones a second apart. The lens keeps showing what it showed; "Connecting…" only appears after 5 s.
     */
    private void nextLink(final int serial) {
        ui.post(() -> {
            if (!alive || serial != linkSerial) return;
            linkSerial++;                                     // this attempt is over
            if (linkDownSince == 0) {
                linkDownSince = SystemClock.elapsedRealtime();
                ui.removeCallbacks(showWaiting);
                ui.postDelayed(showWaiting, 5000);
            }
            ArrayList<String> c = candidates();
            int at = c.indexOf(plain(url));
            // keep the link we had if it was working (first retry), otherwise the next one in the list
            String next = linkFails == 0 && at >= 0 ? c.get(at) : c.get((at + 1) % c.size());
            long wait = linkFails == 0 ? 0 : 1000;
            linkFails++;
            url = withRole(next);
            if (waitingShown) local("", plain(url));
            lastDeviceKey = "";
            ui.postDelayed(() -> { if (alive && !socketOpen) connect(); }, wait);
        });
    }

    /** Called every second: a link that has gone silent is dropped so the next candidate gets its turn. */
    private void checkLink() {
        long nowMs = SystemClock.elapsedRealtime();
        if (!socketOpen && linkDownSince != 0 && nowMs - linkDownSince > 20000) {   // cut off for 20 s: if Wi-Fi is off, that is the way back
            linkDownSince = nowMs;
            try {
                if (!phoneMode && !wifi().isWifiEnabled()) { Log.w(TAG, "no hub link for 20 s and Wi-Fi is off: switching Wi-Fi on"); wifi().setWifiEnabled(true); }
            } catch (Throwable ignored) { }
        }
        WebSocket ws = socket;
        if (ws == null || !socketOpen || !hubPings) return;   // the hub pings every 1.5 s; without that, OkHttp's own pings decide
        long silent = nowMs - lastRxAt, limit = linkIsWifi ? 12000 : 4000;
        if (silent > limit) {
            Log.w(TAG, "link dead: nothing for " + silent / 1000 + " s on " + plain(url));
            lastRxAt = nowMs;
            ws.cancel();                                      // ends in onFailure, which reconnects
        }
    }

    // ---------- Wi-Fi, driven from the phone app through the hub ----------

    private String wifiAsked = "";               // the network we were last asked to join (never its password)
    private String wifiProblem = "";             // "wrong password" and the like, until the next state change says otherwise

    private android.net.wifi.WifiManager wifi() {
        return (android.net.wifi.WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
    }

    /** {"type":"wifi","enable":bool} | {"type":"wifi","join":{"ssid","password"}} | {"type":"wifi","forget":"ssid"} */
    private void applyWifi(final JSONObject m) {
        new Thread(() -> {
            try {
                android.net.wifi.WifiManager wm = wifi();
                if (m.has("enable")) {
                    boolean on = m.optBoolean("enable");
                    if (!on && socketOpen && !isUsb(url)) {
                        // The hub is reached over Wi-Fi right now: switching it off would cut the glasses off for good.
                        wifiProblem = "in use: the glasses reach Maverick over Wi-Fi";
                        Log.w(TAG, "wifi off refused: it is the only link to the hub");
                    } else {
                        Log.i(TAG, "wifi " + (on ? "on" : "off") + ": " + (wm.setWifiEnabled(on) ? "ok" : "refused"));
                    }
                }
                JSONObject join = m.optJSONObject("join");
                if (join != null) {
                    String ssid = join.optString("ssid", ""), password = join.isNull("password") ? "" : join.optString("password", "");
                    if (ssid.isEmpty()) return;
                    Log.i(TAG, "wifi join " + ssid);
                    wifiAsked = ssid; wifiProblem = "";
                    if (!wm.isWifiEnabled()) {
                        wm.setWifiEnabled(true);
                        for (int i = 0; i < 40 && !wm.isWifiEnabled(); i++) Thread.sleep(200);
                    }
                    android.net.wifi.WifiConfiguration conf = new android.net.wifi.WifiConfiguration();
                    conf.SSID = "\"" + ssid + "\"";
                    if (password.isEmpty()) conf.allowedKeyManagement.set(android.net.wifi.WifiConfiguration.KeyMgmt.NONE);
                    else { conf.preSharedKey = "\"" + password + "\""; conf.allowedKeyManagement.set(android.net.wifi.WifiConfiguration.KeyMgmt.WPA_PSK); }
                    int known = networkId(wm, ssid), id = -1;
                    if (known >= 0) { conf.networkId = known; id = wm.updateNetwork(conf); }   // fails for a network another app saved
                    if (id < 0) { conf.networkId = -1; id = wm.addNetwork(conf); }
                    if (id < 0) id = known;                                                     // last resort: the saved one as it is
                    if (id < 0) { wifiProblem = "could not save the network"; Log.w(TAG, "wifi join " + ssid + ": could not save the network"); }
                    else {
                        wm.disconnect();
                        boolean ok = wm.enableNetwork(id, true);
                        wm.reconnect();
                        try { wm.saveConfiguration(); } catch (Throwable ignored) { }
                        Log.i(TAG, "wifi join " + ssid + ": " + (ok ? "connecting" : "refused"));
                    }
                }
                if (m.has("forget") && !m.isNull("forget")) {
                    String ssid = m.optString("forget");
                    int id = networkId(wm, ssid);
                    Log.i(TAG, "wifi forget " + ssid + ": " + (id >= 0 && wm.removeNetwork(id) ? "ok" : "not ours to forget"));
                    try { wm.saveConfiguration(); } catch (Throwable ignored) { }
                }
            } catch (Throwable t) {
                Log.w(TAG, "wifi request failed: " + t.getClass().getSimpleName());
            }
            ui.post(() -> { lastDeviceKey = ""; sendDevice(); });
        }, "maverick-wifi").start();
    }

    private static int networkId(android.net.wifi.WifiManager wm, String ssid) {
        try {
            List<android.net.wifi.WifiConfiguration> all = wm.getConfiguredNetworks();
            if (all != null) for (android.net.wifi.WifiConfiguration c : all) if (("\"" + ssid + "\"").equals(c.SSID)) return c.networkId;
        } catch (Throwable ignored) { }
        return -1;
    }

    /** {"on","connected","ssid","ip","rssi","state"} for the device message. */
    private JSONObject wifiState() {
        JSONObject o = new JSONObject();
        try {
            android.net.wifi.WifiManager wm = wifi();
            boolean on = wm.isWifiEnabled();
            android.net.wifi.WifiInfo wi = on ? wm.getConnectionInfo() : null;
            String ip = on ? wifiAddress() : "";
            String state = !on ? "off" : wi == null || wi.getSupplicantState() == null ? "unknown" : wi.getSupplicantState().name().toLowerCase(java.util.Locale.US);
            boolean connected = on && "completed".equals(state) && !ip.isEmpty();
            String ssid = wi == null || wi.getSSID() == null ? "" : wi.getSSID().replace("\"", "");
            if (ssid.contains("unknown ssid") || ssid.equals("0x")) ssid = connected || !wifiAsked.isEmpty() ? wifiAsked : "";   // hidden without location: what we were asked to join
            if (connected) wifiProblem = "";
            o.put("on", on).put("connected", connected).put("ssid", ssid).put("ip", ip)
             .put("rssi", connected && wi != null ? wi.getRssi() : 0).put("state", wifiProblem.isEmpty() ? state : wifiProblem);
        } catch (Throwable t) {
            try { o.put("on", false).put("connected", false).put("ssid", "").put("ip", "").put("rssi", 0).put("state", "unknown"); } catch (Exception ignored) { }
        }
        return o;
    }

    private void send(String command) {
        try {
            if (socket != null) socket.send(new JSONObject().put("type", "command").put("name", command).toString());
        } catch (Exception e) { Log.w(TAG, "send failed", e); }
    }

    // ---------- Camera (runs on the "maverick-cam" thread) ----------

    /** {"type":"config","width":1280,"height":960,"quality":60,"fps":3} */
    // ---------- Device controls for the phone app: battery, volume, brightness, sleep ----------

    private View sleepCover;
    private volatile boolean asleep;
    private boolean configSinceConnect = true;   // false between a (re)connect and the first config after it
    private float awakeBrightness = -1f;         // window brightness to go back to on waking (-1 = the system's)
    private int batteryLevel = -1, lastVolume = -1;
    private boolean charging;

    private final android.content.BroadcastReceiver deviceEvents = new android.content.BroadcastReceiver() {
        @Override public void onReceive(Context c, android.content.Intent i) {
            if (android.content.Intent.ACTION_BATTERY_CHANGED.equals(i.getAction())) {
                int level = i.getIntExtra(android.os.BatteryManager.EXTRA_LEVEL, -1), scale = i.getIntExtra(android.os.BatteryManager.EXTRA_SCALE, 100);
                int pct = level < 0 || scale <= 0 ? -1 : Math.round(level * 100f / scale);
                boolean plugged = i.getIntExtra(android.os.BatteryManager.EXTRA_PLUGGED, 0) != 0;
                boolean usbNow = (i.getIntExtra(android.os.BatteryManager.EXTRA_PLUGGED, 0) & android.os.BatteryManager.BATTERY_PLUGGED_USB) != 0;
                if (pct == batteryLevel && plugged == charging && usbNow == usbCable) return;
                batteryLevel = pct; charging = plugged;
                usbCable = (i.getIntExtra(android.os.BatteryManager.EXTRA_PLUGGED, 0) & android.os.BatteryManager.BATTERY_PLUGGED_USB) != 0;
                sendDevice();
            } else if (i.getAction() != null && i.getAction().startsWith("android.net.wifi")) {
                if (android.net.wifi.WifiManager.SUPPLICANT_STATE_CHANGED_ACTION.equals(i.getAction())
                        && i.getIntExtra(android.net.wifi.WifiManager.EXTRA_SUPPLICANT_ERROR, 0) == android.net.wifi.WifiManager.ERROR_AUTHENTICATING) {
                    wifiProblem = "wrong password";
                    Log.i(TAG, "wifi: wrong password");
                }
                sendDevice();                    // only goes out if the Wi-Fi state really changed
            } else {                             // volume keys, or anything else that moved the volume
                if (volumePercent() != lastVolume) sendDevice();
            }
        }
    };

    private String lastDeviceKey = "";           // what the hub was last told; "" = tell it again whatever

    private final Runnable deviceTick = new Runnable() {
        @Override public void run() {
            lastDeviceKey = "";
            sendDevice();
            ui.postDelayed(this, 10000);
        }
    };

    private int volumePercent() {
        try {
            AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
            int max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
            return max <= 0 ? 0 : Math.round(am.getStreamVolume(AudioManager.STREAM_MUSIC) * 100f / max);
        } catch (Exception e) { return 0; }
    }

    private static String wifiAddress() {
        try {
            java.net.NetworkInterface nif = java.net.NetworkInterface.getByName("wlan0");
            if (nif != null) for (java.net.InetAddress a : java.util.Collections.list(nif.getInetAddresses()))
                if (a instanceof java.net.Inet4Address && !a.isLoopbackAddress()) return a.getHostAddress();
        } catch (Exception ignored) { }
        return "";
    }

    /** {"type":"device", battery, charging, volume, brightness, model, firmware, app, ip, rssi, asleep}. UI thread. */
    private void sendDevice() {
        WebSocket ws = socket;
        if (ws == null || !socketOpen) return;
        try {
            lastVolume = volumePercent();
            float b = asleep ? awakeBrightness : getWindow().getAttributes().screenBrightness;
            if (b < 0) b = android.provider.Settings.System.getInt(getContentResolver(), android.provider.Settings.System.SCREEN_BRIGHTNESS, 255) / 255f;
            int rssi = 0;
            try {
                android.net.wifi.WifiManager wm = (android.net.wifi.WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
                android.net.wifi.WifiInfo wi = wm == null ? null : wm.getConnectionInfo();
                if (wi != null && wi.getNetworkId() != -1) rssi = wi.getRssi();
            } catch (Exception ignored) { }
            String app = "";
            try { app = getPackageManager().getPackageInfo(getPackageName(), 0).versionName; } catch (Exception ignored) { }
            // Sent when battery, charging, volume, brightness or sleep changed, and on the 10 s timer; never in bursts.
            boolean detecting = detector != null && detector.isRunning();
            JSONObject wf = wifiState();
            String hub = plain(url == null ? "" : url);
            String key = batteryLevel + "|" + charging + "|" + lastVolume + "|" + Math.round(b * 100) + "|" + asleep + "|" + detecting
                    + "|" + hub + "|" + wf.optBoolean("on") + wf.optBoolean("connected") + wf.optString("ssid") + wf.optString("state") + wf.optString("ip");
            if (key.equals(lastDeviceKey)) return;
            lastDeviceKey = key;
            String msg = new JSONObject().put("type", "device").put("battery", batteryLevel).put("charging", charging)
                    .put("volume", lastVolume).put("brightness", Math.round(b * 100) / 100.0)
                    .put("model", Build.MODEL).put("firmware", Build.DISPLAY).put("app", app)
                    .put("ip", wifiAddress()).put("rssi", rssi).put("asleep", asleep)
                    .put("detect", detecting).put("detect_ms", detecting ? Math.round(detector.avgMs()) : 0)
                    .put("link", isUsb(hub) ? "usb" : "wifi").put("hub", hub).put("wifi", wf).toString();
            ws.send(msg);
            Log.i(TAG, "device " + msg);
        } catch (Exception e) { Log.w(TAG, "device message failed", e); }
    }

    /** Asleep: black lens at minimum brightness, no camera, no microphone, screen allowed to time out. The socket stays up. */
    private void setSleep(boolean on) {
        if (on == asleep) return;
        asleep = on;
        Log.i(TAG, on ? "sleep on" : "sleep off");
        WindowManager.LayoutParams lp = getWindow().getAttributes();
        if (on) {
            awakeBrightness = lp.screenBrightness;
            lp.screenBrightness = 0.01f;
            getWindow().setAttributes(lp);
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            sleepCover.setVisibility(View.VISIBLE);
            brickView.setRunning(false);
            voice.stop();
            stopAudio();
            cam.post(this::closeCamera);
            updateDetector();
        } else {
            lp.screenBrightness = awakeBrightness;
            getWindow().setAttributes(lp);
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            sleepCover.setVisibility(View.GONE);
            if (resumed) {
                brickView.setRunning(true);
                cam.post(this::openCamera);
                startVoice();
            }
            updateDetector();
        }
        sendDevice();
    }

    /** A key or a touch while asleep: wake at once and tell the hub. */
    private void wakeByWearer() {
        if (!asleep) return;
        setSleep(false);
        send("wake");
    }

    /** Brightness of this window only (the lens). Remembered, so the next launch starts at the same level. */
    private void setBrightness(float value, boolean remember) {
        if (asleep) {                            // keep the lens dark; use the new level on waking
            awakeBrightness = value;
            if (remember) getSharedPreferences("h", MODE_PRIVATE).edit().putFloat("brightness", value).apply();
            Log.i(TAG, "brightness " + value + " (asleep: applied on waking)");
            return;
        }
        WindowManager.LayoutParams lp = getWindow().getAttributes();
        lp.screenBrightness = value;
        getWindow().setAttributes(lp);
        Log.i(TAG, "brightness " + value);
        if (remember) getSharedPreferences("h", MODE_PRIVATE).edit().putFloat("brightness", value).apply();
    }

    private void applyConfig(JSONObject m) {
        final int w = m.optInt("width", cfgW), h = m.optInt("height", cfgH);
        cfgQuality = Math.max(20, Math.min(95, m.optInt("quality", cfgQuality)));
        if (m.has("brightness") && !m.isNull("brightness")) {      // lens brightness, 0.05 to 1.0; absent = leave as is
            final float b = (float) Math.max(0.05, Math.min(1.0, m.optDouble("brightness", 1.0)));
            ui.post(() -> { setBrightness(b, true); sendDevice(); });
        }
        if (m.has("volume") && !m.isNull("volume")) {              // 0 to 100 of the media volume
            final double v = Math.max(0, Math.min(100, m.optDouble("volume", 100)));
            ui.post(() -> {
                try {
                    AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
                    am.setStreamVolume(AudioManager.STREAM_MUSIC, (int) Math.round(v / 100.0 * am.getStreamMaxVolume(AudioManager.STREAM_MUSIC)), 0);
                    Log.i(TAG, "volume " + Math.round(v));
                } catch (Exception e) { Log.w(TAG, "volume not set", e); }
                sendDevice();
            });
        }
        final JSONArray hu = m.optJSONArray("hub_urls");
        if (hu != null && hu.length() > 0) ui.post(() -> {       // every way to reach this hub; kept for the next launch too
            ArrayList<String> list = new ArrayList<>();
            for (int i = 0; i < hu.length(); i++) { String u = plain(hu.optString(i, "")); if (u.startsWith("ws") && !list.contains(u)) list.add(u); }
            if (list.isEmpty()) return;
            if (!list.equals(hubUrls) || !hubUrlsFresh) {
                hubUrls.clear(); hubUrls.addAll(list); hubUrlsFresh = true;
                getSharedPreferences("h", MODE_PRIVATE).edit().putString("hub_urls", new JSONArray(list).toString()).apply();
                Log.i(TAG, "hub can be reached at " + list);
            }
        });
        // "detect": "glasses" = run the part detector here; "hub" or absent = the hub does it. "detect_version" = the model it serves.
        final boolean wantDetect = "glasses".equals(m.optString("detect", "hub"));
        boolean local = "glasses".equals(m.optString("boxes", "hub"));      // "glasses": this step's boxes are chosen here, from our own detections
        if (local != boxesLocal) {
            boxesLocal = local;
            Log.i(TAG, "boxes chosen " + (local ? "on the glasses" : "by the hub"));
            if (!local) clearLocalBoxes();
        }
        final int version = m.optInt("detect_version", -1);
        ui.post(() -> {
            if (version != detectVersion) detectFailed = false;      // a new model is worth another try
            detectWanted = wantDetect;
            if (version > 0) detectVersion = version;
            updateDetector();
        });
        // "sleep": true/false. After a reconnect, a config without it means awake.
        final boolean hasSleep = m.has("sleep") && !m.isNull("sleep"), wantSleep = m.optBoolean("sleep", false);
        ui.post(() -> {
            boolean firstSinceConnect = !configSinceConnect;
            if (hasSleep) setSleep(wantSleep);
            else if (firstSinceConnect && asleep) setSleep(false);
            configSinceConnect = true;
            if (firstSinceConnect && !asleep) {          // whatever happened before the drop: camera and microphone on
                cam.post(() -> { if (camera == null && cam2 == null) { Log.i(TAG, "camera was off after reconnect: opening"); openCamera(); } });
                startVoice();
            }
        });
        double fps = m.optDouble("fps", 0);
        if (fps > 0) frameIntervalMs = Math.round(1000.0 / Math.max(0.2, Math.min(15, fps)));
        JSONArray cal = m.optJSONArray("calib");
        if (cal != null && cal.length() == 4 && !phoneMode) {
            final float[] cb = {(float) cal.optDouble(0), (float) cal.optDouble(1), (float) cal.optDouble(2), (float) cal.optDouble(3)};
            ui.post(() -> {
                overlay.setCalib(cb);
                float[] now = overlay.calib();
                getSharedPreferences("h", MODE_PRIVATE).edit().putString("calib", now[0] + "," + now[1] + "," + now[2] + "," + now[3]).apply();
                Log.i(TAG, "calib " + Arrays.toString(now));
            });
        }
        cam.post(() -> {
            cfgW = w; cfgH = h;
            Log.i(TAG, "config " + w + "x" + h + " q" + cfgQuality + " every " + frameIntervalMs + " ms");
            if (camera != null) {
                Camera.Size want = pick(camera.getParameters().getSupportedPreviewSizes(), w, h);
                if (want != null && (want.width != camW || want.height != camH)) { closeCamera(); openCamera(); return; }
            } else if (cam2 != null && !cam2.wouldPickSame(w, h)) { closeCamera(); openCamera(); return; }
            sendHello();
        });
    }

    /** {"type":"ar","seq":N,"targets":[{"id","box":[x0,y0,x1,y1],"label","sub","style"}]}: the full list, as seen in frame N. */
    private void applyAr(JSONObject m) {
        if (choiceMode) return;                  // nothing is pointed at while a choice screen is up
        final long seq = m.optLong("seq", -1);
        JSONArray arr = m.optJSONArray("targets");
        int n = arr == null ? 0 : Math.min(4, arr.length()), k = 0;
        String[] ids = new String[n], labels = new String[n], subs = new String[n], styles = new String[n];
        float[][] boxes = new float[n][];
        for (int i = 0; i < n; i++) {
            JSONObject o = arr.optJSONObject(i);
            JSONArray b = o == null ? null : o.optJSONArray("box");
            if (b == null || b.length() != 4) continue;
            ids[k] = o.optString("id", "t" + i);
            boxes[k] = new float[]{(float) b.optDouble(0), (float) b.optDouble(1), (float) b.optDouble(2), (float) b.optDouble(3)};
            labels[k] = str(o, "label"); subs[k] = str(o, "sub"); styles[k] = o.optString("style", "pick");
            k++;
        }
        final String[] fIds = Arrays.copyOf(ids, k), fLabels = Arrays.copyOf(labels, k), fSubs = Arrays.copyOf(subs, k), fStyles = Arrays.copyOf(styles, k);
        final float[][] fBoxes = Arrays.copyOf(boxes, k);
        // "away": parts out of view, as offsets from the view centre in frame widths / heights
        JSONArray aw = m.optJSONArray("away");
        int an = aw == null ? 0 : Math.min(4, aw.length()), ak = 0;
        String[] aLabels = new String[an];
        float[] adx = new float[an], ady = new float[an];
        for (int i = 0; i < an; i++) {
            JSONObject o = aw.optJSONObject(i);
            if (o == null || !o.has("dx") || !o.has("dy")) continue;
            aLabels[ak] = str(o, "label"); adx[ak] = (float) o.optDouble("dx", 0); ady[ak] = (float) o.optDouble("dy", 0);
            ak++;
        }
        final String[] fAway = Arrays.copyOf(aLabels, ak);
        final float[] fAdx = Arrays.copyOf(adx, ak), fAdy = Arrays.copyOf(ady, ak);
        cam.post(() -> {
            if (fAway.length > 0) tracker.setAway(seq < 0 ? frameSeq : seq, fAway, fAdx, fAdy, SystemClock.elapsedRealtime());
            tracker.setGroup(GROUP_HUB, seq < 0 ? frameSeq : seq, fIds, fBoxes, fLabels, fSubs, fStyles, SystemClock.elapsedRealtime());
            overlay.setTargets(tracker.snapshot(), tracker.awaySnapshot());
        });
    }

    /** Closest supported size, preferring the same aspect ratio. */
    private static Camera.Size pick(List<Camera.Size> sizes, int w, int h) {
        Camera.Size best = null;
        double bestScore = Double.MAX_VALUE, aspect = w / (double) h;
        for (Camera.Size s : sizes) {
            double score = Math.abs(s.width * (double) s.height - w * (double) h)
                    + (Math.abs(s.width / (double) s.height - aspect) > 0.03 ? 1e7 : 0);
            if (score < bestScore) { bestScore = score; best = s; }
        }
        return best;
    }

    private static boolean helloSent;

    private void sendHello() {
        WebSocket ws = socket;
        if (ws == null || !socketOpen || (camera == null && cam2 == null)) return;
        try {
            boolean launched = !helloSent;       // only the first hello of this process says the app was just started
            boolean sent = ws.send(new JSONObject().put("type", "hello").put("sizes", new JSONArray(supportedSizes))
                    .put("size", camW + "x" + camH).put("quality", cfgQuality)
                    .put("zoom", Math.round(zoomActual * 100) / 100.0).put("lens", lens)
                    .put("launched", launched).toString());
            if (sent) helloSent = true;
            ui.post(this::sendDevice);
            if (launched) Log.i(TAG, "hello sent with launched=true");
        } catch (Exception e) { Log.w(TAG, "hello failed", e); }
    }

    private int cameraId = 0, openCameraId = 0;
    private float zoomWanted = 1f, zoomActual = 1f;   // below 1 = a phone's ultrawide lens, through Camera2
    private String lens = "main";
    private volatile Cam2Source cam2;
    private boolean cam2Failed;                       // tried and failed: stay on the old API at 1x until relaunched

    private int displayDegrees() {
        int r = getWindowManager().getDefaultDisplay().getRotation();
        return r == Surface.ROTATION_90 ? 90 : r == Surface.ROTATION_180 ? 180 : r == Surface.ROTATION_270 ? 270 : 0;
    }

    private final Cam2Source.Listener cam2Listener = new Cam2Source.Listener() {
        @Override public void onFrame(Cam2Source src, byte[] nv21) {
            if (src == cam2) processFrame(nv21);
        }
        @Override public void onLens(Cam2Source src) {
            if (src != cam2) return;
            zoomActual = src.zoom; lens = src.lens;
            sendHello();
        }
        @Override public void onFailed(Cam2Source src, String why) {
            if (src != cam2) return;
            Log.w(TAG, "camera2 zoom path failed (" + why + "): falling back to the old camera API at 1x");
            cam2Failed = true;
            closeCamera();
            openCamera();
        }
    };

    /** Phone + zoom below 1: Camera2 on the rear camera with that zoom ratio. Returns false to use the old API. */
    private boolean openCamera2() {
        if (!phoneMode || zoomWanted >= 0.999f || cam2Failed) return false;
        if (Build.VERSION.SDK_INT < 30) {
            Log.w(TAG, "zoom " + zoomWanted + " needs Android 11 or newer: using the old camera API at 1x");
            cam2Failed = true;
            return false;
        }
        try {
            Cam2Source s = Cam2Source.open(this, cam, previewSurface, cfgW, cfgH, zoomWanted, displayDegrees(), cam2Listener);
            cam2 = s;
            camW = s.width; camH = s.height;
            camRotation = s.turn == 180 ? 180 : 0;
            if (s.turn == 90 || s.turn == 270) Log.w(TAG, "screen is not landscape: frames will be sideways (turn " + s.turn + ")");
            zoomActual = s.zoom; lens = s.lens;
            supportedSizes.clear();
            supportedSizes.addAll(s.sizes);
            tracker.reset();
            statsAt = SystemClock.elapsedRealtime(); framesSent = 0; bytesSent = 0;
            Log.i(TAG, "camera open (camera2) " + camW + "x" + camH + " zoom " + zoomActual + " turn=" + camRotation);
            ui.post(this::layoutPreview);
            sendHello();
            return true;
        } catch (Exception e) {
            Log.w(TAG, "camera2 zoom path not available (" + e + "): using the old camera API at 1x");
            cam2Failed = true;
            cam2 = null;
            return false;
        }
    }

    private void openCamera() {
        if (!resumed || asleep || camera != null || cam2 != null) return;
        if (phoneMode && previewSurface == null) return;   // opens when the preview surface is ready
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "camera permission not granted yet");
            return;
        }
        if (openCamera2()) return;
        zoomActual = 1f; lens = "main";
        try {
            int id = cameraId < Camera.getNumberOfCameras() ? cameraId : 0;
            Camera c;
            try { c = Camera.open(id); }
            catch (RuntimeException e) { Log.w(TAG, "camera " + id + " would not open, using camera 0", e); id = 0; c = Camera.open(0); }
            openCameraId = id;
            Camera.Parameters p = c.getParameters();
            Log.i(TAG, "camera id " + id + " of " + Camera.getNumberOfCameras() + ", view angle " + p.getHorizontalViewAngle() + " x " + p.getVerticalViewAngle());
            List<Camera.Size> sizes = p.getSupportedPreviewSizes();
            supportedSizes.clear();
            for (Camera.Size s : sizes) supportedSizes.add(s.width + "x" + s.height);
            Camera.Size best = pick(sizes, cfgW, cfgH);
            if (best != null) p.setPreviewSize(best.width, best.height);
            p.setPreviewFormat(ImageFormat.NV21);
            List<String> modes = p.getSupportedFocusModes();
            if (modes != null) {
                if (modes.contains(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE)) p.setFocusMode(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE);
                else if (modes.contains(Camera.Parameters.FOCUS_MODE_CONTINUOUS_VIDEO)) p.setFocusMode(Camera.Parameters.FOCUS_MODE_CONTINUOUS_VIDEO);
            }
            try { c.setParameters(p); } catch (RuntimeException e) { Log.w(TAG, "setParameters rejected, using defaults", e); }
            Camera.Size size = c.getParameters().getPreviewSize();
            camW = size.width; camH = size.height;
            camRotation = 0;
            if (phoneMode) {
                Camera.CameraInfo ci = new Camera.CameraInfo();
                Camera.getCameraInfo(openCameraId, ci);
                int r = getWindowManager().getDefaultDisplay().getRotation();
                int degrees = r == Surface.ROTATION_90 ? 90 : r == Surface.ROTATION_180 ? 180 : r == Surface.ROTATION_270 ? 270 : 0;
                int turn = (ci.orientation - degrees + 360) % 360;
                c.setDisplayOrientation(turn);
                camRotation = turn == 180 ? 180 : 0;
                if (turn == 90 || turn == 270) Log.w(TAG, "screen is not landscape: frames will be sideways (turn " + turn + ")");
                c.setPreviewTexture(previewSurface);
            } else {
                dummyTexture = new SurfaceTexture(10);   // off-screen: nothing is drawn on the lens
                c.setPreviewTexture(dummyTexture);
            }
            tracker.reset();
            int bufSize = camW * camH * 3 / 2;
            c.addCallbackBuffer(new byte[bufSize]);
            c.addCallbackBuffer(new byte[bufSize]);
            c.addCallbackBuffer(new byte[bufSize]);
            c.setPreviewCallbackWithBuffer(this::onFrame);
            c.setErrorCallback((err, cc) -> {
                Log.w(TAG, "camera error " + err);
                cam.post(() -> { closeCamera(); cam.postDelayed(this::openCamera, 1500); });
            });
            c.startPreview();
            camera = c;
            statsAt = SystemClock.elapsedRealtime(); framesSent = 0; bytesSent = 0;
            Log.i(TAG, "camera open " + camW + "x" + camH + " focus=" + c.getParameters().getFocusMode() + " turn=" + camRotation + " sizes=" + supportedSizes);
            if (phoneMode) ui.post(this::layoutPreview);
            sendHello();
        } catch (Exception e) {
            Log.w(TAG, "camera open failed, retrying", e);
            closeCamera();
            cam.postDelayed(this::openCamera, 2000);
        }
    }

    private void closeCamera() {
        Camera c = camera;
        camera = null;
        if (c != null) {
            try { c.setPreviewCallbackWithBuffer(null); c.stopPreview(); } catch (Exception ignored) { }
            try { c.release(); } catch (Exception ignored) { }
        }
        Cam2Source c2 = cam2;
        cam2 = null;
        if (c2 != null) c2.close();
        if (dummyTexture != null) { dummyTexture.release(); dummyTexture = null; }
        tracker.reset();
        if (overlay != null) overlay.setTargets(tracker.snapshot(), tracker.awaySnapshot());
    }

    /** Turn an NV21 picture upside down in place (phone held in reverse landscape). */
    private static void rotate180(byte[] d, int w, int h) {
        int n = w * h;
        for (int i = 0, j = n - 1; i < j; i++, j--) { byte t = d[i]; d[i] = d[j]; d[j] = t; }
        for (int a = n, b = n + n / 2 - 2; a < b; a += 2, b -= 2) {
            byte t = d[a]; d[a] = d[b]; d[b] = t;
            t = d[a + 1]; d[a + 1] = d[b + 1]; d[b + 1] = t;
        }
    }

    /**
     * Called on the camera thread for every preview frame. The tracker sees all of them; about 3 a second
     * are copied to the encoder thread and sent to the hub with their frame counter.
     */
    private void onFrame(byte[] data, Camera c) {
        try {
            if (data == null || c != camera) return;
            processFrame(data);
        } finally {
            if (data != null && c == camera) {
                try { c.addCallbackBuffer(data); } catch (Exception ignored) { }
            }
        }
    }

    /** One NV21 picture from either camera API, on the camera thread. */
    private void processFrame(byte[] data) {
        try {
            if (data.length < camW * camH * 3 / 2) return;
            final long now = SystemClock.elapsedRealtime();
            lastCameraFrameAt = now;
            final long seq = ++frameSeq;
            if (camRotation == 180) rotate180(data, camW, camH);
            tracker.onFrame(data, camW, camH, seq, now);
            Detector det = detector;
            if (det != null && det.isRunning()) det.offer(data, camW, camH, seq);
            overlay.setTargets(tracker.snapshot(), tracker.awaySnapshot());
            if (now - trackLogAt >= 5000) {
                trackLogAt = now;
                Log.i(TAG, "tracker " + String.format("%.1f fps, %.2f ms/frame, %d target(s), seq %d", tracker.fps(), tracker.msPerFrame(),
                        tracker.snapshot().length, seq));
            }
            final WebSocket ws = socket;
            // Send only while at most about half a frame is still waiting, so frames stay fresh on slow Wi-Fi.
            // Frames are the first thing to go when the link is slow: one is only sent when nothing is waiting, so
            // detections and commands never sit behind a pile of pictures. Over Wi-Fi: 2 a second at most, smaller files.
            final boolean gentle = linkIsWifi;
            if (!encBusy && now - lastFrameAt >= Math.max(frameIntervalMs, gentle ? 500 : 0) - 15 && ws != null && socketOpen
                    && ws.queueSize() < 2048) {
                lastFrameAt = now;
                final int w = camW, h = camH, q = gentle ? Math.min(cfgQuality, 45) : cfgQuality, len = w * h * 3 / 2;
                if (encBuf == null || encBuf.length != len) encBuf = new byte[len];
                final byte[] copy = encBuf;
                System.arraycopy(data, 0, copy, 0, len);
                encBusy = true;
                enc.post(() -> {
                    try {
                        ByteArrayOutputStream out = new ByteArrayOutputStream(w * h / 6);
                        new YuvImage(copy, ImageFormat.NV21, w, h, null).compressToJpeg(new Rect(0, 0, w, h), q, out);
                        byte[] jpeg = out.toByteArray();
                        String b64 = Base64.encodeToString(jpeg, Base64.NO_WRAP);
                        lastB64Len = b64.length();
                        ws.send("{\"type\":\"frame\",\"seq\":" + seq + ",\"jpeg\":\"" + b64 + "\"}");
                        framesSent++; bytesSent += jpeg.length;
                        long t = SystemClock.elapsedRealtime();
                        if (t - statsAt >= 5000) {
                            Log.i(TAG, "frames " + w + "x" + h + " " + String.format("%.1f", framesSent * 1000f / (t - statsAt))
                                    + " fps, avg " + (bytesSent / Math.max(1, framesSent)) + " bytes, q" + q + ", queue " + ws.queueSize()
                                    + (gentle ? ", over wifi" : ", over usb"));
                            statsAt = t; framesSent = 0; bytesSent = 0;
                        }
                    } catch (Exception e) {
                        Log.w(TAG, "frame failed", e);
                    } finally { encBusy = false; }
                });
            }
        } catch (Exception e) {
            Log.w(TAG, "frame failed", e);
        }
    }

    // ---------- Keys ----------

    private final Runnable singleTap = () -> { };   // the old tap-to-peek timer; nothing is scheduled on it any more

    /** Tap = talk: Maverick listens at once. A tap while it is listening cancels. Slides change the step. */
    private void tap() {
        if (calibMode) { send("calib_capture"); return; }
        if (choiceMode) { send("choice_select"); return; }
        togglePtt();
    }

    /** A slide changes the step. If Maverick was listening, that is dropped first; a slide never starts listening. */
    private void slide(String command) {
        if (pttState == PTT_LISTENING) voice.pttCancel();
        send(command);
    }

    @Override
    public boolean onKeyDown(int code, KeyEvent e) {
        Log.i(TAG, "key " + code + " " + KeyEvent.keyCodeToString(code) + (choiceMode ? " (choice screen)" : ""));
        if (asleep) {                            // any key wakes; it does not also act as a tap or a slide
            if (e.getRepeatCount() == 0) wakeByWearer();
            return true;
        }
        if (choiceMode) return choiceKey(code, e);
        switch (code) {
            case KeyEvent.KEYCODE_ENTER: case KeyEvent.KEYCODE_DPAD_CENTER: case KeyEvent.KEYCODE_NUMPAD_ENTER:
                if (e.getRepeatCount() == 0) tap();
                return true;
            // Slides on the touchpad change the step: one way is next, the other is back.
            case KeyEvent.KEYCODE_DPAD_RIGHT: case KeyEvent.KEYCODE_DPAD_DOWN: case KeyEvent.KEYCODE_VOLUME_UP:
            case KeyEvent.KEYCODE_PAGE_DOWN: case KeyEvent.KEYCODE_MEDIA_NEXT: case KeyEvent.KEYCODE_SPACE:
            case KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE: case KeyEvent.KEYCODE_HEADSETHOOK:
                if (e.getRepeatCount() == 0) slide("next");
                return true;
            case KeyEvent.KEYCODE_DPAD_LEFT: case KeyEvent.KEYCODE_DPAD_UP: case KeyEvent.KEYCODE_VOLUME_DOWN:
            case KeyEvent.KEYCODE_PAGE_UP: case KeyEvent.KEYCODE_MEDIA_PREVIOUS: case KeyEvent.KEYCODE_BACK:
                if (e.getRepeatCount() == 0) slide("back");
                return true;
            case KeyEvent.KEYCODE_R: send("repeat"); return true;
            case KeyEvent.KEYCODE_M: send("more"); return true;
            default:
                if (!e.isSystem()) Log.i(TAG, "key " + code + " is not mapped: ignored");
                return super.onKeyDown(code, e);
        }
    }

    /** Keys while a choice screen is up: a slide either way moves the selection, a tap confirms. */
    private boolean choiceKey(int code, KeyEvent e) {
        if (e.getRepeatCount() > 0) return true;
        switch (code) {
            case KeyEvent.KEYCODE_ENTER: case KeyEvent.KEYCODE_DPAD_CENTER: case KeyEvent.KEYCODE_NUMPAD_ENTER:
                send("choice_select"); return true;
            case KeyEvent.KEYCODE_DPAD_LEFT: case KeyEvent.KEYCODE_DPAD_UP: case KeyEvent.KEYCODE_VOLUME_DOWN:
            case KeyEvent.KEYCODE_PAGE_UP: case KeyEvent.KEYCODE_MEDIA_PREVIOUS: case KeyEvent.KEYCODE_BACK:
                send("choice_prev"); return true;
            case KeyEvent.KEYCODE_DPAD_RIGHT: case KeyEvent.KEYCODE_DPAD_DOWN: case KeyEvent.KEYCODE_VOLUME_UP:
            case KeyEvent.KEYCODE_PAGE_DOWN: case KeyEvent.KEYCODE_MEDIA_NEXT: case KeyEvent.KEYCODE_SPACE:
                send("choice_next"); return true;
            default:
                if (e.isSystem()) return super.onKeyDown(code, e);     // power, home and the like stay the system's
                // the slider's other direction may send a code nobody has seen yet: count it as "next"
                Log.i(TAG, "key " + code + " " + KeyEvent.keyCodeToString(code) + " is not mapped: treated as choice_next");
                send("choice_next");
                return true;
        }
    }

    @Override
    public void onBackPressed() { /* BACK means "previous step"; never exit the HUD */ }

    @Override
    protected void onDestroy() {
        if (detector != null) detector.stop();
        ui.removeCallbacks(cameraWatch);
        ui.removeCallbacks(comeToFront);
        ui.removeCallbacks(deviceTick);
        try { unregisterReceiver(deviceEvents); } catch (Exception ignored) { }
        alive = false;
        socketOpen = false;
        if (socket != null) socket.cancel();
        stopAudio();
        cam.post(this::closeCamera);
        camThread.quitSafely();
        encThread.quitSafely();
        if (tones != null) tones.release();
        super.onDestroy();
    }
}
