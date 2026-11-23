package com.maverick.glasses;

/**
 * Keeps boxes locked onto things in the camera picture between the hub's slow, late detections.
 * Pure Java (no Android imports) so it can be tested on a desktop JVM.
 *
 * Every camera frame becomes a small grey thumbnail kept in a ring (about 5 s). For each frame we
 * estimate how the whole picture moved since the previous one. When the hub says "this box, as seen
 * in frame N", we cut a template out of thumbnail N, carry the box forward to now by adding up the
 * stored picture motion, and lock on with a template match. After that each new frame is: predict
 * with the picture motion, refine with a small template match.
 *
 * Not thread safe: call everything from the camera thread. {@link #snapshot()} returns an immutable
 * array that other threads may read.
 */
public final class BoxTracker {
    public static final int TW = 320;            // thumbnail width; height follows the camera aspect
    private static final int RING = 160;         // about 5 s at 30 fps
    private static final int COARSE = 4;         // coarse image = thumbnail / 4
    private static final int COARSE_RANGE = 6;   // +-6 coarse px = +-24 thumbnail px per frame
    private static final int FINE_RANGE = 3;
    private static final int TRACK_RANGE = 4;    // per-frame template search, thumbnail px
    private static final int LOCK_RANGE = 12;    // lock-on search after carrying a box forward
    private static final int TEMPLATE_MAX = 40;
    private static final int PULL = 2;           // cost per px^2 away from the prediction, in mean-squared grey levels
    private static final long DROP_NO_REFRESH_MS = 4000, DROP_UNSURE_MS = 1500;

    /** What the overlay draws. Coordinates are normalised to the full camera frame. */
    public static final class Target {
        public final String id, label, sub, style;
        public final float cx, cy, w, h;
        public final float vx, vy;       // picture motion, normalised units per second
        public final boolean unsure;
        public final float score;        // match error relative to the template's contrast; lower is better
        Target(String id, String label, String sub, String style, float cx, float cy, float w, float h,
               float vx, float vy, boolean unsure, float score) {
            this.id = id; this.label = label; this.sub = sub; this.style = style;
            this.cx = cx; this.cy = cy; this.w = w; this.h = h; this.vx = vx; this.vy = vy;
            this.unsure = unsure; this.score = score;
        }
    }

    private static final class Track {
        String id, label, sub, style;
        int group;                       // 0 = from the hub, 1 = chosen on the glasses
        float px, py;                    // box centre in the newest thumbnail
        float bw, bh;                    // box size in thumbnail px (from the hub)
        byte[] tmpl = new byte[TEMPLATE_MAX * TEMPLATE_MAX];
        int tw, th, step;                // template samples and their spacing in thumbnail px
        float offX, offY;                // template top-left relative to the box centre
        float tstd;                      // template contrast (standard deviation)
        long lastArMs, unsureSinceMs;
        boolean unsure;
        float score;
    }

    private static final Target[] NONE = new Target[0];

    /** A part that is outside the camera's view: its offset from the view centre, in frame widths and heights. */
    public static final class Away {
        public final String label;
        public final float dx, dy;
        Away(String label, float dx, float dy) { this.label = label; this.dx = dx; this.dy = dy; }
    }

    private static final class AwayTrack {
        String label;
        float px, py;                    // where the part would be in the newest thumbnail (outside it)
        long lastMs;                     // last hub message that mentioned it
    }

    private static final Away[] NO_AWAY = new Away[0];
    private static final long DROP_AWAY_MS = 1200;
    private AwayTrack[] aways = new AwayTrack[0];
    private volatile Away[] publishedAway = NO_AWAY;

    public Away[] awaySnapshot() { return publishedAway; }

    /**
     * Parts the hub knows are out of view, as offsets from the view centre in frame seq. They are carried to the
     * newest frame with the stored picture motion and then follow every frame, so the arrow swings as the head turns.
     * Entries that a message leaves out stay for 1.2 s, then go.
     */
    public void setAway(long seq, String[] labels, float[] dx, float[] dy, long nowMs) {
        int n = labels == null ? 0 : labels.length;
        if (n == 0) return;
        float h = Math.max(1, th);
        int back = count > 0 ? framesBack(seq) : 0;
        float sx = 0, sy = 0;
        for (int k = back - 1; k >= 0; k--) {
            int i = (head - k + RING) % RING;
            sx += shiftX[i]; sy += shiftY[i];
        }
        java.util.ArrayList<AwayTrack> next = new java.util.ArrayList<>(java.util.Arrays.asList(aways));
        for (int i = 0; i < n; i++) {
            AwayTrack t = null;
            for (AwayTrack old : next) if (old.label.equals(labels[i])) t = old;
            if (t == null) { t = new AwayTrack(); t.label = labels[i]; next.add(t); }
            t.px = TW * (0.5f + dx[i]) + sx;
            t.py = h * (0.5f + dy[i]) + sy;
            t.lastMs = nowMs;
        }
        aways = next.toArray(new AwayTrack[0]);
        publishAway();
    }

    /** How many frames ago frame seq was: 0 when it is the newest or newer, everything we have when it has left the ring. */
    private int framesBack(long seq) {
        for (int k = 0; k < count; k++) if (seqs[(head - k + RING) % RING] == seq) return k;
        return seq > seqs[head] ? 0 : count - 1;
    }

    private void publishAway() {
        if (aways.length == 0) { if (publishedAway.length != 0) publishedAway = NO_AWAY; return; }
        Away[] out = new Away[aways.length];
        float h = Math.max(1, th);
        for (int i = 0; i < aways.length; i++) out[i] = new Away(aways[i].label, aways[i].px / TW - 0.5f, aways[i].py / h - 0.5f);
        publishedAway = out;
    }

    private int th;                      // thumbnail height
    private int cw, ch;                  // coarse size
    private int srcW, srcH;
    private final byte[][] thumbs = new byte[RING][];
    private final long[] seqs = new long[RING];
    private final float[] shiftX = new float[RING], shiftY = new float[RING];   // motion from the previous frame to this one
    private int head = -1, count;        // head = index of the newest frame
    private byte[] coarsePrev, coarseCur;
    private final int[] fine = new int[(2 * FINE_RANGE + 1) * (2 * FINE_RANGE + 1)];
    private final float[] sub = new float[2];
    private long lastMs;
    private float vx, vy;                // smoothed picture motion, thumbnail px per second

    private Track[] tracks = new Track[0];
    private volatile Target[] published = NONE;

    // stats
    private long statFrames, statNanos, statStartMs;
    private float lastFps, lastMsPerFrame;

    public int thumbHeight() { return th; }
    public float fps() { return lastFps; }
    public float msPerFrame() { return lastMsPerFrame; }
    public Target[] snapshot() { return published; }
    /** Motion of the newest frame relative to the one before, in thumbnail px (for tests). */
    public float lastShiftX() { return head < 0 ? 0 : shiftX[head]; }
    public float lastShiftY() { return head < 0 ? 0 : shiftY[head]; }

    public void reset() {
        head = -1; count = 0; tracks = new Track[0]; published = NONE; vx = vy = 0;
        aways = new AwayTrack[0]; publishedAway = NO_AWAY;
    }

    // ------------------------------------------------------------------ frames

    /**
     * @param y    the camera picture, 8-bit luma, row-major, at least w*h bytes (an NV21 buffer works as is)
     * @param seq  the frame counter that the hub will quote back
     */
    public void onFrame(byte[] y, int w, int h, long seq, long nowMs) {
        long t0 = System.nanoTime();
        if (w != srcW || h != srcH) {
            srcW = w; srcH = h;
            th = Math.max(COARSE * (2 * COARSE_RANGE + 4), (TW * h / w) & ~3);
            cw = TW / COARSE; ch = th / COARSE;
            for (int i = 0; i < RING; i++) thumbs[i] = null;
            coarsePrev = new byte[cw * ch]; coarseCur = new byte[cw * ch];
            reset();
        }
        int prev = head;
        head = (head + 1) % RING;
        if (count < RING) count++;
        if (thumbs[head] == null) thumbs[head] = new byte[TW * th];
        byte[] cur = thumbs[head];
        makeThumb(y, w, h, cur);
        byte[] swap = coarsePrev; coarsePrev = coarseCur; coarseCur = swap;
        makeCoarse(cur, coarseCur);
        seqs[head] = seq;

        float dx = 0, dy = 0;
        if (prev >= 0 && count > 1) {
            globalShift(thumbs[prev], cur);
            dx = sub[0]; dy = sub[1];
        }
        shiftX[head] = dx; shiftY[head] = dy;
        long dt = nowMs - lastMs;
        if (prev >= 0 && dt > 0 && dt < 500) {
            vx += (dx * 1000f / dt - vx) * 0.4f;
            vy += (dy * 1000f / dt - vy) * 0.4f;
        } else { vx = vy = 0; }
        lastMs = nowMs;

        boolean dropped = false;
        for (Track t : tracks) {
            t.px += dx; t.py += dy;
            refine(t, cur, TRACK_RANGE, nowMs);
            if (nowMs - t.lastArMs > DROP_NO_REFRESH_MS || (t.unsure && nowMs - t.unsureSinceMs > DROP_UNSURE_MS)) {
                t.id = null; dropped = true;
            }
        }
        if (dropped) compact();
        publish();
        if (aways.length > 0) {
            int keep = 0;
            for (AwayTrack a : aways) {
                a.px += dx; a.py += dy;
                if (nowMs - a.lastMs <= DROP_AWAY_MS) keep++;
            }
            if (keep != aways.length) {
                AwayTrack[] next = new AwayTrack[keep];
                int k = 0;
                for (AwayTrack a : aways) if (nowMs - a.lastMs <= DROP_AWAY_MS) next[k++] = a;
                aways = next;
            }
            publishAway();
        }

        statFrames++; statNanos += System.nanoTime() - t0;
        if (statStartMs == 0) statStartMs = nowMs;
        if (nowMs - statStartMs >= 2000) {
            lastFps = statFrames * 1000f / (nowMs - statStartMs);
            lastMsPerFrame = statNanos / 1e6f / statFrames;
            statFrames = 0; statNanos = 0; statStartMs = nowMs;
        }
    }

    /** 2x2-sampled reduction of the luma plane to TW x th. */
    private void makeThumb(byte[] y, int w, int h, byte[] out) {
        int xs = (w << 12) / TW, ys = (h << 12) / th;      // 20.12 fixed point steps
        int i = 0;
        for (int ty = 0; ty < th; ty++) {
            int r0 = ((ty * ys) >> 12) * w;
            int r1 = Math.min(h - 1, (ty * ys + (ys >> 1)) >> 12) * w;
            for (int tx = 0; tx < TW; tx++) {
                int x0 = (tx * xs) >> 12, x1 = Math.min(w - 1, (tx * xs + (xs >> 1)) >> 12);
                out[i++] = (byte) (((y[r0 + x0] & 255) + (y[r0 + x1] & 255) + (y[r1 + x0] & 255) + (y[r1 + x1] & 255)) >> 2);
            }
        }
    }

    private void makeCoarse(byte[] thumb, byte[] out) {
        int i = 0;
        for (int cy = 0; cy < ch; cy++) {
            for (int cx = 0; cx < cw; cx++) {
                int s = 0, base = cy * COARSE * TW + cx * COARSE;
                for (int j = 0; j < COARSE; j++) {
                    int r = base + j * TW;
                    s += (thumb[r] & 255) + (thumb[r + 1] & 255) + (thumb[r + 2] & 255) + (thumb[r + 3] & 255);
                }
                out[i++] = (byte) (s >> 4);
            }
        }
    }

    /** Whole-picture motion from prev to cur: content at prev (x, y) is at cur (x + dx, y + dy). Result in sub[]. */
    private void globalShift(byte[] prev, byte[] cur) {
        // 1. coarse exhaustive search
        final int R = COARSE_RANGE;
        int best = Integer.MAX_VALUE, bdx = 0, bdy = 0;
        for (int dy = -R; dy <= R; dy++) {
            for (int dx = -R; dx <= R; dx++) {
                int sad = 0;
                // a small bias toward "no motion" settles ties on flat scenes
                int bias = (Math.abs(dx) + Math.abs(dy)) * 2;
                for (int yy = R; yy < ch - R && sad < best; yy++) {
                    int p = yy * cw + R, c = (yy + dy) * cw + R + dx;
                    for (int n = cw - 2 * R; n > 0; n--) {
                        int d = (coarsePrev[p++] & 255) - (coarseCur[c++] & 255);
                        sad += d < 0 ? -d : d;
                    }
                }
                sad += bias;
                if (sad < best) { best = sad; bdx = dx; bdy = dy; }
            }
        }
        // 2. refine on the thumbnail around the coarse answer (every 4th pixel, squared error)
        final int F = FINE_RANGE, side = 2 * F + 1, m = COARSE * R + F + 1;
        int cx0 = bdx * COARSE, cy0 = bdy * COARSE;
        int fb = Integer.MAX_VALUE, fi = 0;
        for (int oy = -F; oy <= F; oy++) {
            for (int ox = -F; ox <= F; ox++) {
                int ssd = 0, sx = cx0 + ox, sy = cy0 + oy;
                for (int yy = m; yy < th - m; yy += 4) {
                    int p = yy * TW + m, c = (yy + sy) * TW + m + sx;
                    for (int xx = m; xx < TW - m; xx += 4, p += 4, c += 4) {
                        int d = (prev[p] & 255) - (cur[c] & 255);
                        ssd += d * d;
                    }
                }
                int idx = (oy + F) * side + ox + F;
                fine[idx] = ssd;
                if (ssd < fb) { fb = ssd; fi = idx; }
            }
        }
        int ox = fi % side - F, oy = fi / side - F;
        sub[0] = cx0 + ox + parabola(fine, side, fi, 1, ox, F);
        sub[1] = cy0 + oy + parabola(fine, side, fi, side, oy, F);
    }

    /** Sub-pixel offset of the minimum along one axis of an error grid. */
    private static float parabola(int[] e, int side, int i, int stride, int pos, int range) {
        if (pos <= -range || pos >= range) return 0;
        float a = e[i - stride], b = e[i], c = e[i + stride];
        float den = a - 2 * b + c;
        if (den <= 1e-3f) return 0;
        float o = 0.5f * (a - c) / den;
        return o < -0.5f ? -0.5f : o > 0.5f ? 0.5f : o;
    }

    // ------------------------------------------------------------------ targets

    /**
     * The hub's newest detections: the complete list. Boxes are normalised [x0, y0, x1, y1] as seen in frame seq.
     * A known id is re-anchored; ids that are missing are dropped.
     */
    public void setTargets(long seq, String[] ids, float[][] boxes, String[] labels, String[] subs, String[] styles, long nowMs) {
        setGroup(0, seq, ids, boxes, labels, subs, styles, nowMs);
    }

    /**
     * Targets come from two places: the hub (group 0) and the glasses' own detector (group 1). A call replaces the
     * targets of ITS group only. A null box means "keep tracking this one as it is" (it stays, and counts as refreshed).
     */
    public void setGroup(int group, long seq, String[] ids, float[][] boxes, String[] labels, String[] subs, String[] styles, long nowMs) {
        int n = ids == null ? 0 : ids.length;
        java.util.ArrayList<Track> next = new java.util.ArrayList<>(tracks.length + n);
        for (Track old : tracks) if (old.id != null && old.group != group) next.add(old);
        for (int i = 0; i < n; i++) {
            Track t = null;
            for (Track old : tracks) if (old.id != null && old.group == group && old.id.equals(ids[i])) t = old;
            if (boxes[i] == null) {                       // no new position: the existing track carries on
                if (t != null) { t.label = labels[i]; t.sub = subs[i]; t.style = styles[i]; t.lastArMs = nowMs; next.add(t); }
                continue;
            }
            if (t == null) t = new Track();
            t.group = group;
            t.id = ids[i]; t.label = labels[i]; t.sub = subs[i]; t.style = styles[i];
            t.lastArMs = nowMs;
            next.add(t);
            if (count > 0) anchor(t, seq, boxes[i], nowMs);
            else {      // no frames yet: just place it
                t.px = (boxes[i][0] + boxes[i][2]) * 0.5f * TW; t.py = (boxes[i][1] + boxes[i][3]) * 0.5f * Math.max(1, th);
                t.bw = (boxes[i][2] - boxes[i][0]) * TW; t.bh = (boxes[i][3] - boxes[i][1]) * Math.max(1, th);
                t.tw = 0; t.unsure = false;
            }
        }
        tracks = next.toArray(new Track[0]);
        publish();
    }

    private void anchor(Track t, long seq, float[] box, long nowMs) {
        // find frame seq in the ring; if it has left, use the oldest; if it is newer than anything, the newest
        int oldest = (head - count + 1 + RING) % RING;
        int at = -1, back = 0;
        for (int k = 0; k < count; k++) {
            int i = (head - k + RING) % RING;
            if (seqs[i] == seq) { at = i; back = k; break; }
        }
        if (at < 0) {
            if (seq > seqs[head]) { at = head; back = 0; } else { at = oldest; back = count - 1; }
        }
        float x0 = box[0] * TW, y0 = box[1] * th, x1 = box[2] * TW, y1 = box[3] * th;
        t.bw = Math.max(2, x1 - x0); t.bh = Math.max(2, y1 - y0);
        float cx = (x0 + x1) * 0.5f, cy = (y0 + y1) * 0.5f;

        // template: the box plus 20% context, at least 24 px, cut from frame "at"
        float halfW = Math.max(12, t.bw * 0.6f), halfH = Math.max(12, t.bh * 0.6f);
        int tx0 = Math.max(0, Math.round(cx - halfW)), ty0 = Math.max(0, Math.round(cy - halfH));
        int tx1 = Math.min(TW, Math.round(cx + halfW)), ty1 = Math.min(th, Math.round(cy + halfH));
        t.tw = 0;
        if (tx1 - tx0 >= 8 && ty1 - ty0 >= 8) {
            int step = Math.max(1, (Math.max(tx1 - tx0, ty1 - ty0) + TEMPLATE_MAX - 1) / TEMPLATE_MAX);
            int tw = (tx1 - tx0) / step, tth = (ty1 - ty0) / step;
            byte[] src = thumbs[at];
            long s = 0, s2 = 0;
            for (int j = 0; j < tth; j++)
                for (int i = 0; i < tw; i++) {
                    int v = src[(ty0 + j * step) * TW + tx0 + i * step] & 255;
                    t.tmpl[j * tw + i] = (byte) v;
                    s += v; s2 += v * v;
                }
            int n = tw * tth;
            double mean = s / (double) n;
            t.tstd = (float) Math.sqrt(Math.max(0, s2 / (double) n - mean * mean));
            t.tw = tw; t.th = tth; t.step = step;
            t.offX = tx0 - cx; t.offY = ty0 - cy;
        }

        // Walk the box from frame "at" to the newest frame: add the stored picture motion and re-lock on the
        // template every few frames, exactly as live tracking would have done. One big jump followed by a wide
        // search locked onto the neighbouring key of a keyboard when the head had also moved closer or tilted.
        t.px = cx; t.py = cy;
        t.unsure = false; t.unsureSinceMs = 0;
        int stride = back > 40 ? 3 : back > 20 ? 2 : 1;
        for (int k = back - 1, n = 0; k >= 0; k--) {
            int i = (head - k + RING) % RING;
            t.px += shiftX[i]; t.py += shiftY[i];
            if (++n % stride == 0 || k == 0) refine(t, thumbs[i], TRACK_RANGE, nowMs);
        }
        if (back == 0) refine(t, thumbs[head], 1, nowMs);
    }

    /** Template match around the predicted place; moves the track there when the match is believable. */
    private void refine(Track t, byte[] img, int range, long nowMs) {
        if (t.tw == 0) return;                       // no template: follow the picture motion only
        int bx = Math.round(t.px + t.offX), by = Math.round(t.py + t.offY);
        int spanX = (t.tw - 1) * t.step, spanY = (t.th - 1) * t.step;
        // shrink the search so every candidate stays inside the picture; none left = the thing is out of view
        int loX = Math.max(-range, -bx), hiX = Math.min(range, TW - 1 - spanX - bx);
        int loY = Math.max(-range, -by), hiY = Math.min(range, th - 1 - spanY - by);
        if (loX > hiX || loY > hiY) return;
        // Pick the best place, with a small pull toward the prediction. Without the pull a template that is
        // mostly one straight edge slides along that edge from frame to frame (every place on it matches).
        int n = t.tw * t.th;
        long bestCost = Long.MAX_VALUE;
        int best = 0, box = 0, boy = 0;
        boolean any = false;
        for (int oy = loY; oy <= hiY; oy++)
            for (int ox = loX; ox <= hiX; ox++) {
                int e = error(t, img, bx + ox, by + oy);
                long cost = e + (long) n * PULL * (ox * ox + oy * oy);
                if (cost < bestCost) { bestCost = cost; best = e; box = ox; boy = oy; any = true; }
            }
        if (!any) return;
        // sub-pixel: errors of the four neighbours
        float fx = 0, fy = 0;
        if (box > loX && box < hiX) {
            float a = error(t, img, bx + box - 1, by + boy), c = error(t, img, bx + box + 1, by + boy);
            float den = a - 2f * best + c;
            if (den > 1e-3f) fx = clampHalf(0.5f * (a - c) / den);
        }
        if (boy > loY && boy < hiY) {
            float a = error(t, img, bx + box, by + boy - 1), c = error(t, img, bx + box, by + boy + 1);
            float den = a - 2f * best + c;
            if (den > 1e-3f) fy = clampHalf(0.5f * (a - c) / den);
        }
        float rms = (float) Math.sqrt(best / (double) (t.tw * t.th));
        t.score = rms / (t.tstd + 1f);
        boolean good = rms < 0.6f * t.tstd + 5f;
        if (good) {
            t.px = bx + box + fx - t.offX;
            t.py = by + boy + fy - t.offY;
            t.unsure = false;
        } else if (!t.unsure) {
            t.unsure = true; t.unsureSinceMs = nowMs;
        }
    }

    private static float clampHalf(float v) { return v < -0.5f ? -0.5f : v > 0.5f ? 0.5f : v; }

    /** Brightness-compensated squared error between the template and the picture at (x, y). */
    private int error(Track t, byte[] img, int x, int y) {
        int n = t.tw * t.th, sum = 0, sq = 0, k = 0, step = t.step;
        for (int j = 0; j < t.th; j++) {
            int p = (y + j * step) * TW + x;
            for (int i = 0; i < t.tw; i++, p += step) {
                int d = (img[p] & 255) - (t.tmpl[k++] & 255);
                sum += d; sq += d * d;
            }
        }
        return sq - (int) ((long) sum * sum / n);
    }

    private void compact() {
        int n = 0;
        for (Track t : tracks) if (t.id != null) n++;
        Track[] next = new Track[n];
        n = 0;
        for (Track t : tracks) if (t.id != null) next[n++] = t;
        tracks = next;
    }

    private void publish() {
        if (tracks.length == 0) { if (published.length != 0) published = NONE; return; }
        // When the hub and the glasses both have a box for the same id, the glasses' own wins: the hub's copy only
        // shows while the glasses have none (it fills in parts the small on-glasses model misses).
        int n = 0;
        for (Track t : tracks) if (!shadowed(t)) n++;
        Target[] out = new Target[n];
        float h = Math.max(1, th);
        int k = 0;
        for (Track t : tracks) {
            if (shadowed(t)) continue;
            out[k++] = new Target(t.id, t.label, t.sub, t.style, t.px / TW, t.py / h, t.bw / TW, t.bh / h,
                    vx / TW, vy / h, t.unsure, t.score);
        }
        published = out;
    }

    private boolean shadowed(Track t) {
        if (t.group != 0) return false;
        for (Track o : tracks) if (o.group != 0 && o.id != null && o.id.equals(t.id)) return true;
        return false;
    }
}
