package com.maverick.glasses;

import android.os.SystemClock;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.FloatBuffer;
import java.util.Collections;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicBoolean;

import ai.onnxruntime.OnnxTensor;
import ai.onnxruntime.OrtEnvironment;
import ai.onnxruntime.OrtSession;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

/**
 * The part detector, running on the glasses. The hub serves the model (ONNX) and a meta.json that says
 * everything about it: file, sizes, classes, thresholds, threads. One thread takes the newest camera frame,
 * turns it straight from NV21 into the model's input, runs the model, and reports the boxes with the frame
 * number they belong to. It never queues: when a run ends it starts on whatever is newest.
 *
 * Measured on the glasses (2026-11-21): plain CPU, one thread, normal priority, no core pinning is the fastest
 * that leaves the tracker at 30 fps; XNNPACK and more threads were no better, and this kernel refuses a pin to
 * the big core alone.
 */
final class Detector {
    interface Listener {
        /** A finished detection, already in the wire format: {"type":"dets",...}. Called on the detector thread. */
        void onDets(String json);
        /** The same detections as arrays (valid only during the call): class, confidence, [x0,y0,x1,y1] in unit coordinates. */
        void onResult(long seq, int n, String[] cls, float[] conf, float[][] box);
        /** The detector could not start or gave up; it is off now. */
        void onError(String text);
        /** Tracker cost right now, for the log (ms per frame). */
        float trackerMs();
    }

    private static final String TAG = "MaverickHUD";
    private static final int MAX_BOXES = 200;

    /** One loaded model: what meta.json said, the session, and the buffers that fit it. */
    private static final class Model {
        int version, width, height, contentHeight, threads;
        float padValue, conf, iou;
        String inputName, outputName, file;
        String[] classes;
        OrtSession session;
        OnnxTensor input;
        FloatBuffer inputBuf;
        float[] chw, out;
        int[] mapX, mapUv;
        int mappedW, mappedH;
        long loadMs;

        void close() {
            try { if (input != null) input.close(); } catch (Throwable ignored) { }
            try { if (session != null) session.close(); } catch (Throwable ignored) { }
            input = null; session = null;
        }
    }

    private final File dir;
    private final OkHttpClient http;
    private final Listener listener;
    private final float[] lut = new float[256];
    private volatile String metaUrl;
    private OrtEnvironment env;

    private volatile int generation;            // bumps on every start and stop; a thread of an older generation ends
    private volatile boolean active;
    private volatile Model pending;             // a newer model, built in the background, waiting to be swapped in
    private final AtomicBoolean loading = new AtomicBoolean();
    private volatile int version = -1;
    private volatile float avgMs;

    // newest camera frame, handed over by the camera thread
    private final Object lock = new Object();
    private byte[] frame;
    private int frameW, frameH;
    private long frameSeq;
    private boolean wanted, has;

    // results of the last run
    private final float[] bx0 = new float[MAX_BOXES], by0 = new float[MAX_BOXES], bx1 = new float[MAX_BOXES], by1 = new float[MAX_BOXES], bconf = new float[MAX_BOXES];
    private final int[] bcls = new int[MAX_BOXES], order = new int[MAX_BOXES];
    private final boolean[] dropped = new boolean[MAX_BOXES];
    private int found;
    private double preMs, inferMs, postMs;
    private final StringBuilder sb = new StringBuilder(512);
    private final String[] rCls = new String[MAX_BOXES];
    private final float[] rConf = new float[MAX_BOXES];
    private final float[][] rBox = new float[MAX_BOXES][4];

    Detector(File filesDir, OkHttpClient http, Listener listener) {
        this.dir = filesDir; this.http = http; this.listener = listener;
        for (int i = 0; i < 256; i++) lut[i] = i / 255f;
    }

    boolean isRunning() { return active; }
    float avgMs() { return avgMs; }
    /** Version of the model in use; -1 until one is loaded. */
    int version() { return version; }

    synchronized void start(String metaUrl) {
        this.metaUrl = metaUrl;
        if (active) return;
        active = true;
        final int gen = ++generation;
        Thread t = new Thread(() -> loop(gen), "maverick-detect");
        t.start();
    }

    synchronized void stop() {
        if (!active) return;
        active = false;
        generation++;
        synchronized (lock) { lock.notifyAll(); }
    }

    /** The hub may be serving a different model now: look, and if so build it in the background and swap it in. */
    void refresh(String metaUrl) {
        this.metaUrl = metaUrl;
        if (!active || !loading.compareAndSet(false, true)) return;
        final int gen = generation;
        new Thread(() -> {
            try {
                Model m = load(version);
                if (m == null) return;                              // same version: nothing to do
                if (gen == generation && active) pending = m; else m.close();
            } catch (Throwable t) {
                Log.w(TAG, "detector: newer model not loaded, keeping v" + version + ": " + t);
            } finally { loading.set(false); }
        }, "maverick-detect-load").start();
    }

    /** Camera thread, every frame: copies the picture only when the detector is waiting for one. */
    void offer(byte[] nv21, int w, int h, long seq) {
        if (!wanted) return;
        synchronized (lock) {
            if (!wanted) return;
            int len = w * h * 3 / 2;
            if (frame == null || frame.length != len) frame = new byte[len];
            System.arraycopy(nv21, 0, frame, 0, len);
            frameW = w; frameH = h; frameSeq = seq;
            wanted = false; has = true;
            lock.notifyAll();
        }
    }

    private boolean awaitFrame(int gen) throws InterruptedException {
        synchronized (lock) {
            has = false; wanted = true;
            long until = SystemClock.elapsedRealtime() + 2000;
            while (gen == generation && !has) {
                long left = until - SystemClock.elapsedRealtime();
                if (left <= 0) { wanted = false; return false; }
                lock.wait(left);
            }
            if (gen != generation) wanted = false;
            return gen == generation && has;
        }
    }

    // ------------------------------------------------------------------ the thread

    private void loop(int gen) {
        Model cur = null;
        try {
            loading.set(true);
            try { cur = load(-1); } finally { loading.set(false); }
            version = cur.version;
            Log.i(TAG, "detector model v" + cur.version + " " + cur.width + "x" + cur.height + " " + cur.file + " loaded in " + cur.loadMs + " ms");
            long statAt = SystemClock.elapsedRealtime();
            double sPre = 0, sInfer = 0, sPost = 0; int runs = 0, lastFound = 0, fails = 0;
            while (gen == generation) {
                Model next = pending;
                if (next != null) {                                 // a newer model is ready: swap between two frames
                    pending = null;
                    cur.close();
                    cur = next;
                    version = cur.version;
                    Log.i(TAG, "detector model v" + cur.version + " " + cur.width + "x" + cur.height + " " + cur.file + " loaded in " + cur.loadMs + " ms");
                    sPre = sInfer = sPost = 0; runs = 0; statAt = SystemClock.elapsedRealtime();
                }
                if (!awaitFrame(gen)) continue;
                long seq = frameSeq;
                try {
                    detect(cur);
                    fails = 0;
                } catch (Throwable t) {
                    fails++;
                    Log.w(TAG, "detector run failed (" + fails + " in a row): " + t);
                    if (fails >= 5) { listener.onError("detector failed 5 times in a row: " + t); return; }
                    Thread.sleep(1000);
                    continue;
                }
                String js = json(cur, seq);
                listener.onResult(seq, found, rCls, rConf, rBox);       // the glasses choose their own boxes from these
                listener.onDets(js);
                sPre += preMs; sInfer += inferMs; sPost += postMs; runs++; lastFound = found;
                long now = SystemClock.elapsedRealtime();
                if (now - statAt >= 5000) {
                    avgMs = (float) ((sPre + sInfer + sPost) / runs);
                    Log.i(TAG, String.format(Locale.US, "detect %dx%d avg %.0f ms (pre %.0f / infer %.0f / post %.1f), %.1f fps, %d found, tracker %.1f ms/frame",
                            cur.width, cur.height, avgMs, sPre / runs, sInfer / runs, sPost / runs, runs * 1000.0 / (now - statAt), lastFound, listener.trackerMs()));
                    statAt = now; sPre = sInfer = sPost = 0; runs = 0;
                }
            }
        } catch (InterruptedException ignored) {
        } catch (Throwable t) {
            Log.w(TAG, "detector not started: " + t);
            if (gen == generation) listener.onError("detector model not available: " + t.getMessage());
        } finally {
            synchronized (this) { if (gen == generation) active = false; }
            if (cur != null) cur.close();
            Model p = pending; pending = null;
            if (p != null) p.close();
            avgMs = 0;
        }
    }

    // ------------------------------------------------------------------ model

    /**
     * Reads meta.json from the hub, fetches the model file it names if it is not on the glasses yet, and builds a
     * session for it. Returns null when the hub's version is the one already in use.
     */
    private Model load(int have) throws Exception {
        long t0 = SystemClock.elapsedRealtime();
        String url = metaUrl;
        JSONObject j;
        try (Response r = http.newCall(new Request.Builder().url(url).build()).execute()) {
            if (!r.isSuccessful() || r.body() == null) throw new IllegalStateException("meta.json: hub answered " + r.code());
            j = new JSONObject(r.body().string());
        }
        Model m = new Model();
        m.version = j.getInt("version");
        if (m.version == have) return null;
        m.file = j.optString("file", "model.onnx");
        m.inputName = j.optString("input", "images");
        m.outputName = j.optString("output", "output0");
        m.width = j.getInt("width"); m.height = j.getInt("height");
        m.contentHeight = j.optInt("content_height", m.height);
        m.padValue = (float) j.optDouble("pad_value", 114);
        m.conf = (float) j.optDouble("conf", 0.35); m.iou = (float) j.optDouble("iou", 0.5);
        m.threads = Math.max(1, j.optInt("threads", 1));
        JSONArray cs = j.getJSONArray("classes");
        m.classes = new String[cs.length()];
        for (int i = 0; i < m.classes.length; i++) m.classes[i] = cs.getString(i);

        final File model = new File(dir, "detector-v" + m.version + ".onnx");
        if (!model.exists() || model.length() == 0) {
            File tmp = new File(dir, "detector.download");
            try (Response r = http.newCall(new Request.Builder().url(url.substring(0, url.lastIndexOf('/') + 1) + m.file).build()).execute()) {
                if (!r.isSuccessful() || r.body() == null) throw new IllegalStateException(m.file + ": hub answered " + r.code());
                try (FileOutputStream o = new FileOutputStream(tmp); java.io.InputStream in = r.body().byteStream()) {
                    byte[] buf = new byte[65536];
                    for (int n; (n = in.read(buf)) > 0; ) o.write(buf, 0, n);
                }
            }
            if (!tmp.renameTo(model)) throw new IllegalStateException("could not store the model");
            Log.i(TAG, "detector model v" + m.version + " " + m.file + " downloaded, " + model.length() / 1024 + " KB");
        }
        if (env == null) env = OrtEnvironment.getEnvironment();
        OrtSession.SessionOptions so = new OrtSession.SessionOptions();
        so.setIntraOpNumThreads(m.threads);
        so.setInterOpNumThreads(1);
        so.setOptimizationLevel(OrtSession.SessionOptions.OptLevel.ALL_OPT);
        try { so.addConfigEntry("session.intra_op.allow_spinning", "0"); } catch (Throwable ignored) { }   // idle workers must not burn the tracker's cores
        m.session = env.createSession(model.getAbsolutePath(), so);
        so.close();
        int plane = m.width * m.height;
        m.chw = new float[3 * plane];
        java.util.Arrays.fill(m.chw, m.padValue / 255f);            // the rows below the picture stay at the pad value
        m.inputBuf = ByteBuffer.allocateDirect(3 * plane * 4).order(ByteOrder.nativeOrder()).asFloatBuffer();
        m.input = OnnxTensor.createTensor(env, m.inputBuf, new long[]{1, 3, m.height, m.width});
        m.loadMs = SystemClock.elapsedRealtime() - t0;
        // older model files are no longer needed (the one in use stays until the swap; it is already open)
        File[] old = dir.listFiles((d, n) -> (n.startsWith("detector-v") || n.startsWith("bench-")) && !n.equals(model.getName()));
        if (old != null) for (File f : old) f.delete();
        return m;
    }

    // ------------------------------------------------------------------ one frame

    private void detect(Model m) throws Exception {
        long t0 = System.nanoTime();
        fill(m, frame, frameW, frameH);
        m.inputBuf.rewind();
        m.inputBuf.put(m.chw);
        m.inputBuf.rewind();
        long t1 = System.nanoTime();
        try (OrtSession.Result r = m.session.run(Collections.singletonMap(m.inputName, m.input))) {
            long t2 = System.nanoTime();
            OnnxTensor o = (OnnxTensor) r.get(m.outputName).orElse(r.get(0));
            long[] shape = o.getInfo().getShape();                  // [1, 4 + classes, anchors]
            FloatBuffer fb = o.getFloatBuffer();
            int n = fb.remaining();
            if (m.out == null || m.out.length != n) m.out = new float[n];
            fb.get(m.out);
            decode(m, (int) shape[1], (int) shape[2]);
            long t3 = System.nanoTime();
            preMs = (t1 - t0) / 1e6; inferMs = (t2 - t1) / 1e6; postMs = (t3 - t2) / 1e6;
        }
    }

    /** NV21 camera frame to RGB floats at width x content_height, by picking pixels (no bitmap, no JPEG). */
    private void fill(Model m, byte[] nv21, int w, int h) {
        final int W = m.width, CH = m.contentHeight, plane = m.width * m.height;
        if (m.mappedW != w || m.mappedH != h || m.mapX == null) {
            m.mapX = new int[W]; m.mapUv = new int[W];
            for (int x = 0; x < W; x++) {
                int sx = Math.min(w - 1, (int) ((x + 0.5f) * w / W));
                m.mapX[x] = sx; m.mapUv[x] = sx & ~1;
            }
            m.mappedW = w; m.mappedH = h;
        }
        final float[] dst = m.chw; final float[] l = lut; final int[] mx = m.mapX, mu = m.mapUv;
        final int uvBase = w * h;
        for (int y = 0; y < CH; y++) {
            int sy = Math.min(h - 1, (int) ((y + 0.5f) * h / CH));
            int yRow = sy * w, uvRow = uvBase + (sy >> 1) * w;
            int o = y * W;
            for (int x = 0; x < W; x++, o++) {
                int Y = nv21[yRow + mx[x]] & 255;
                int V = (nv21[uvRow + mu[x]] & 255) - 128, U = (nv21[uvRow + mu[x] + 1] & 255) - 128;
                int r = Y + ((1436 * V) >> 10), g = Y - ((352 * U + 731 * V) >> 10), b = Y + ((1815 * U) >> 10);
                dst[o] = l[r < 0 ? 0 : r > 255 ? 255 : r];
                dst[plane + o] = l[g < 0 ? 0 : g > 255 ? 255 : g];
                dst[2 * plane + o] = l[b < 0 ? 0 : b > 255 ? 255 : b];
            }
        }
    }

    /** Best class per anchor, boxes to unit coordinates of the camera frame, then per-class NMS. */
    private void decode(Model m, int rows, int anchors) {
        final float[] o = m.out; final int nc = Math.min(m.classes.length, rows - 4);
        int n = 0;
        for (int i = 0; i < anchors && n < MAX_BOXES; i++) {
            int best = -1; float bs = m.conf;
            for (int c = 0; c < nc; c++) { float s = o[(4 + c) * anchors + i]; if (s >= bs) { bs = s; best = c; } }
            if (best < 0) continue;
            float cx = o[i], cy = o[anchors + i], w = o[2 * anchors + i], h = o[3 * anchors + i];
            bx0[n] = clamp((cx - w / 2) / m.width); bx1[n] = clamp((cx + w / 2) / m.width);
            by0[n] = clamp((cy - h / 2) / m.contentHeight); by1[n] = clamp((cy + h / 2) / m.contentHeight);
            bconf[n] = bs; bcls[n] = best; dropped[n] = false; order[n] = n;
            n++;
        }
        // order by confidence (few boxes: insertion sort, no allocation)
        for (int a = 1; a < n; a++) {
            int k = order[a]; int b = a - 1;
            while (b >= 0 && bconf[order[b]] < bconf[k]) { order[b + 1] = order[b]; b--; }
            order[b + 1] = k;
        }
        for (int a = 0; a < n; a++) {
            int i = order[a];
            if (dropped[i]) continue;
            for (int b = a + 1; b < n; b++) {
                int j = order[b];
                if (dropped[j] || bcls[j] != bcls[i]) continue;
                float ix = Math.min(bx1[i], bx1[j]) - Math.max(bx0[i], bx0[j]), iy = Math.min(by1[i], by1[j]) - Math.max(by0[i], by0[j]);
                if (ix <= 0 || iy <= 0) continue;
                float inter = ix * iy, uni = (bx1[i] - bx0[i]) * (by1[i] - by0[i]) + (bx1[j] - bx0[j]) * (by1[j] - by0[j]) - inter;
                if (inter / uni > m.iou) dropped[j] = true;
            }
        }
        found = n;
    }

    private static float clamp(float v) { return v < 0 ? 0 : v > 1 ? 1 : v; }

    private String json(Model m, long seq) {
        sb.setLength(0);
        sb.append("{\"type\":\"dets\",\"seq\":").append(seq).append(",\"found\":[");
        int kept = 0;
        for (int a = 0; a < found; a++) {
            int i = order[a];
            if (dropped[i]) continue;
            rCls[kept] = m.classes[bcls[i]]; rConf[kept] = bconf[i];
            rBox[kept][0] = bx0[i]; rBox[kept][1] = by0[i]; rBox[kept][2] = bx1[i]; rBox[kept][3] = by1[i];
            if (kept++ > 0) sb.append(',');
            sb.append("{\"cls\":\"").append(m.classes[bcls[i]]).append("\",\"conf\":").append(Math.round(bconf[i] * 100) / 100f)
              .append(",\"box\":[").append(r3(bx0[i])).append(',').append(r3(by0[i])).append(',').append(r3(bx1[i])).append(',').append(r3(by1[i])).append("]}");
        }
        sb.append("],\"ms\":").append(Math.round(preMs + inferMs + postMs)).append(",\"w\":").append(m.width).append(",\"h\":").append(m.height)
          .append(",\"v\":").append(m.version).append('}');
        found = kept;      // what the log calls "found": after NMS
        return sb.toString();
    }

    private static float r3(float v) { return Math.round(v * 1000) / 1000f; }
}
