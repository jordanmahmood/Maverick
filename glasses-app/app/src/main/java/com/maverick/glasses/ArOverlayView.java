package com.maverick.glasses;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.DashPathEffect;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.graphics.Typeface;
import android.os.SystemClock;
import android.text.TextPaint;
import android.view.View;

import java.util.ArrayList;

/**
 * The AR layer: boxes that stay on real objects. The tracker (camera thread) hands over the newest box
 * positions in camera coordinates; this view maps them to display pixels, glides toward them every vsync
 * and draws a rounded outline with a label pill, or an edge chevron when the thing is outside the display.
 * Also draws the calibration screen.
 */
public class ArOverlayView extends View {
    static final int ACCENT = Color.rgb(0x39, 0xFF, 0x14);   // the one accent colour: neon green
    static final int RED = Color.rgb(0xFF, 0x5C, 0x5C);      // only for "remove this" and a wrong part
    private static final float EASE_S = 0.05f, FADE_S = 0.15f;

    private static final class Box {
        String id, label = "", sub = "", style = "pick";
        final RectF r = new RectF();
        float alpha, alphaGoal;
        boolean placed, seen;
    }

    private final float dp;
    private final Paint stroke = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final TextPaint pillText = new TextPaint(Paint.ANTI_ALIAS_FLAG);
    private final TextPaint calibText = new TextPaint(Paint.ANTI_ALIAS_FLAG);
    private final Path path = new Path();
    private final RectF tmp = new RectF();
    private final DashPathEffect dash;
    private final ArrayList<Box> boxes = new ArrayList<>();

    /** An arrow at the edge of the view for a part that is out of the camera's sight. */
    private static final class Arrow {
        String label;
        float x, y, tx, ty, alpha, alphaGoal;
        boolean placed, seen;
    }
    private final ArrayList<Arrow> arrows = new ArrayList<>();
    private volatile BoxTracker.Away[] aways = new BoxTracker.Away[0];

    private volatile BoxTracker.Target[] targets = new BoxTracker.Target[0];
    private volatile boolean idle = true;
    private float[] calib = {0.33f, 0.30f, 0.67f, 0.64f};   // part of the camera frame that the display covers
    private RectF previewRect;                               // phone mode: where the whole camera frame is drawn
    private boolean calibMode;
    private float bottomInset, topInset;                     // keep-clear bands (instruction bar, status bar), px
    private float leadSec;                                   // look-ahead that hides camera latency on a see-through lens
    private long lastDraw;

    public ArOverlayView(Context c) {
        super(c);
        dp = getResources().getDisplayMetrics().density;
        float sp = getResources().getDisplayMetrics().scaledDensity;
        stroke.setStyle(Paint.Style.STROKE);
        stroke.setStrokeJoin(Paint.Join.ROUND);
        stroke.setStrokeCap(Paint.Cap.ROUND);
        fill.setStyle(Paint.Style.FILL);
        pillText.setTextSize(14 * sp);
        pillText.setTypeface(Typeface.create(Typeface.DEFAULT, 600, false));
        calibText.setTextSize(15 * sp);
        calibText.setColor(Color.WHITE);
        dash = new DashPathEffect(new float[]{10 * dp, 7 * dp}, 0);
    }

    /** Called from the camera thread after every frame. */
    public void setTargets(BoxTracker.Target[] t, BoxTracker.Away[] away) {
        targets = t;
        aways = away;
        if (idle && (t.length > 0 || away.length > 0)) { idle = false; postInvalidateOnAnimation(); }
    }

    public void setCalib(float[] c) {
        if (c != null && c.length == 4 && c[2] - c[0] > 0.02f && c[3] - c[1] > 0.02f) { calib = c.clone(); wake(); }
    }

    public float[] calib() { return calib.clone(); }

    /** Phone mode: the rectangle (view px) where the full camera frame is shown. Replaces calib. */
    public void setPreviewRect(RectF r) { previewRect = r == null ? null : new RectF(r); wake(); }

    public void setCalibMode(boolean on) { calibMode = on; wake(); }

    public void setInsets(float top, float bottom) {
        if (top == topInset && bottom == bottomInset) return;
        topInset = top; bottomInset = bottom; wake();
    }

    public void setLead(float seconds) { leadSec = seconds; }

    private void wake() { idle = false; postInvalidateOnAnimation(); }

    /** Where the full camera frame lands on this view. */
    private void frameRect(RectF out) {
        if (previewRect != null) { out.set(previewRect); return; }
        float w = getWidth() / (calib[2] - calib[0]), h = getHeight() / (calib[3] - calib[1]);
        out.set(-calib[0] * w, -calib[1] * h, -calib[0] * w + w, -calib[1] * h + h);
    }

    @Override
    protected void onDraw(Canvas c) {
        long now = SystemClock.uptimeMillis();
        float dt = lastDraw == 0 ? 0.016f : Math.min(0.1f, (now - lastDraw) / 1000f);
        lastDraw = now;
        if (calibMode) { drawCalib(c); idle = true; lastDraw = 0; return; }

        frameRect(tmp);
        float fl = tmp.left, ft = tmp.top, fw = tmp.width(), fh = tmp.height();
        float ease = 1f - (float) Math.exp(-dt / EASE_S);
        for (Box b : boxes) b.seen = false;
        for (BoxTracker.Target t : targets) {
            Box b = null;
            for (Box x : boxes) if (x.id.equals(t.id)) b = x;
            if (b == null) { b = new Box(); b.id = t.id; boxes.add(b); }
            b.seen = true;
            b.label = t.label == null ? "" : t.label;
            b.sub = t.sub == null ? "" : t.sub;
            b.style = t.style == null ? "pick" : t.style;
            float cx = fl + (t.cx + t.vx * leadSec) * fw, cy = ft + (t.cy + t.vy * leadSec) * fh;
            float hw = Math.max(8 * dp, t.w * fw / 2), hh = Math.max(8 * dp, t.h * fh / 2);
            if (!b.placed) { b.r.set(cx - hw, cy - hh, cx + hw, cy + hh); b.placed = true; }
            else {
                b.r.left += (cx - hw - b.r.left) * ease; b.r.top += (cy - hh - b.r.top) * ease;
                b.r.right += (cx + hw - b.r.right) * ease; b.r.bottom += (cy + hh - b.r.bottom) * ease;
            }
            b.alphaGoal = t.unsure ? 0.45f : 1f;
        }
        float fade = dt / FADE_S;
        for (int i = boxes.size() - 1; i >= 0; i--) {
            Box b = boxes.get(i);
            if (!b.seen) b.alphaGoal = 0;
            b.alpha += Math.max(-fade, Math.min(fade, b.alphaGoal - b.alpha));
            if (!b.seen && b.alpha <= 0.01f) { boxes.remove(i); continue; }
            drawBox(c, b, now);
        }
        drawArrows(c, now, fl, ft, fw, fh, ease, fade);
        if (boxes.isEmpty() && targets.length == 0 && arrows.isEmpty() && aways.length == 0) { idle = true; lastDraw = 0; }
        else postInvalidateOnAnimation();
    }

    /**
     * Parts that are out of the camera's view: a chevron on the edge of the view nearest that direction, nudging
     * toward it once a second, with the part's name beside it. Gone as soon as the part comes inside the view.
     */
    private void drawArrows(Canvas c, long now, float fl, float ft, float fw, float fh, float ease, float fade) {
        // the area arrows live in: the camera picture on a phone, the whole display on the glasses, minus the bars
        float left = 0, right = getWidth(), top = topInset, bottom = getHeight() - bottomInset;
        if (previewRect != null) {
            left = Math.max(left, previewRect.left); right = Math.min(right, previewRect.right);
            top = Math.max(top, previewRect.top); bottom = Math.min(bottom, previewRect.bottom);
        }
        float m = 22 * dp;
        for (Arrow a : arrows) a.seen = false;
        for (BoxTracker.Away w : aways) {
            Arrow a = null;
            for (Arrow x : arrows) if (x.label.equals(w.label)) a = x;
            boolean inside = Math.abs(w.dx) <= 0.5f && Math.abs(w.dy) <= 0.5f;
            if (a == null) {
                if (inside) continue;
                a = new Arrow(); a.label = w.label; arrows.add(a);
            }
            a.seen = true;
            a.tx = fl + (0.5f + w.dx) * fw; a.ty = ft + (0.5f + w.dy) * fh;      // where the part is, in display px
            float px = Math.max(left + m, Math.min(right - m, a.tx)), py = Math.max(top + m, Math.min(bottom - m, a.ty));
            if (!a.placed) { a.x = px; a.y = py; a.placed = true; }
            else { a.x += (px - a.x) * ease; a.y += (py - a.y) * ease; }
            a.alphaGoal = inside ? 0f : 1f;
        }
        for (int i = arrows.size() - 1; i >= 0; i--) {
            Arrow a = arrows.get(i);
            if (!a.seen) a.alphaGoal = 0;
            a.alpha += Math.max(-fade, Math.min(fade, a.alphaGoal - a.alpha));
            if (a.alphaGoal == 0 && a.alpha <= 0.01f) { if (!a.seen) arrows.remove(i); continue; }
            int alpha = Math.round(255 * Math.max(0, Math.min(1, a.alpha)));
            double ang = Math.atan2(a.ty - a.y, a.tx - a.x);
            float cos = (float) Math.cos(ang), sin = (float) Math.sin(ang);
            float nudge = 4 * dp * (0.5f - 0.5f * (float) Math.cos(now % 1000 * (2 * Math.PI / 1000.0)));
            float cx = a.x + cos * nudge, cy = a.y + sin * nudge, s = 11 * dp;
            stroke.setColor(ACCENT); stroke.setAlpha(alpha); stroke.setPathEffect(null); stroke.setStrokeWidth(3.5f * dp);
            c.save();
            c.rotate((float) Math.toDegrees(ang), cx, cy);
            path.rewind();
            path.moveTo(cx - s * 0.5f, cy - s); path.lineTo(cx + s * 0.5f, cy); path.lineTo(cx - s * 0.5f, cy + s);
            c.drawPath(path, stroke);
            c.restore();
            if (a.label.isEmpty()) continue;
            float pw = pillWidth(a.label), ph = pillHeight(), gap = 8 * dp, x, y;
            if (Math.abs(cos) >= Math.abs(sin)) { x = cos > 0 ? a.x - s - gap - pw : a.x + s + gap; y = a.y - ph / 2; }
            else { x = a.x - pw / 2; y = sin > 0 ? a.y - s - gap - ph : a.y + s + gap; }
            x = Math.max(left + 2 * dp, Math.min(right - pw - 2 * dp, x));
            y = Math.max(top + 2 * dp, Math.min(bottom - ph - 2 * dp, y));
            drawPill(c, a.label, false, ACCENT, x, y, alpha);
        }
    }

    private void drawBox(Canvas c, Box b, long now) {
        float w = getWidth(), bottom = getHeight() - bottomInset;
        float cx = b.r.centerX(), cy = b.r.centerY();
        boolean place = "place".equals(b.style);
        // "place" breathes between 65% and 100% every 1.2 s
        float a = b.alpha * (place ? 0.825f + 0.175f * (float) Math.sin(now * (2 * Math.PI / 1200.0)) : 1f);
        int alpha = Math.round(255 * Math.max(0, Math.min(1, a)));
        String text = b.sub.isEmpty() ? b.label : b.label + " · " + b.sub;
        int color = "remove".equals(b.style) ? RED : ACCENT;   // "remove" looks like "pick", in red

        if (cx < 0 || cx > w || cy < topInset || cy > bottom) { drawChevron(c, text, place, color, cx, cy, w, bottom, alpha); return; }

        c.save();
        c.clipRect(0, topInset, w, bottom);        // never over the instruction bar, wherever it is
        RectF r = b.r;
        float rad = Math.min(10 * dp, Math.min(r.width(), r.height()) / 2);
        stroke.setColor(color);
        stroke.setPathEffect(place ? dash : null);
        stroke.setStrokeWidth(7 * dp); stroke.setAlpha(Math.round(alpha * 0.22f));     // soft glow
        c.drawRoundRect(r, rad, rad, stroke);
        stroke.setStrokeWidth(2.5f * dp); stroke.setAlpha(alpha);
        c.drawRoundRect(r, rad, rad, stroke);
        stroke.setPathEffect(null);
        c.restore();

        if (text.isEmpty()) return;
        float pw = pillWidth(text), ph = pillHeight(), gap = 6 * dp;
        float x = Math.max(2 * dp, Math.min(w - pw - 2 * dp, r.left));
        float y = r.top - gap - ph;
        if (y < topInset + 2 * dp) y = r.bottom + gap;                       // no room above: go below
        y = Math.max(topInset + 2 * dp, Math.min(bottom - ph - 2 * dp, y));  // stay on screen, above the bar
        drawPill(c, text, place, color, x, y, alpha);
    }

    private float pillWidth(String text) { return pillText.measureText(text) + 16 * dp; }

    private float pillHeight() {
        Paint.FontMetrics fm = pillText.getFontMetrics();
        return fm.descent - fm.ascent + 8 * dp;
    }

    /** Label pill: filled for "pick", outlined for "place". */
    private void drawPill(Canvas c, String text, boolean place, int color, float x, float y, int alpha) {
        float pw = pillWidth(text), ph = pillHeight();
        tmp.set(x, y, x + pw, y + ph);
        if (place) {
            stroke.setColor(color); stroke.setAlpha(alpha); stroke.setPathEffect(null); stroke.setStrokeWidth(1.5f * dp);
            tmp.inset(0.75f * dp, 0.75f * dp);
            c.drawRoundRect(tmp, ph / 2, ph / 2, stroke);
            pillText.setColor(color);
        } else {
            fill.setColor(color); fill.setAlpha(alpha);
            c.drawRoundRect(tmp, ph / 2, ph / 2, fill);
            pillText.setColor(Color.BLACK);
        }
        pillText.setAlpha(alpha);
        c.drawText(text, x + 8 * dp, y + 4 * dp - pillText.getFontMetrics().ascent, pillText);
    }

    /** The thing is outside the display: a small arrow on the nearest edge says which way to look, with its label. */
    private void drawChevron(Canvas c, String text, boolean place, int color, float cx, float cy, float w, float bottom, int alpha) {
        float m = 18 * dp, s = 7 * dp;
        float px = Math.max(m, Math.min(w - m, cx)), py = Math.max(topInset + m, Math.min(bottom - m, cy));
        float ang = (float) Math.toDegrees(Math.atan2(cy - py, cx - px));
        stroke.setColor(color); stroke.setAlpha(alpha); stroke.setPathEffect(null); stroke.setStrokeWidth(3 * dp);
        c.save();
        c.rotate(ang, px, py);
        path.rewind();
        path.moveTo(px - s * 0.5f, py - s); path.lineTo(px + s * 0.5f, py); path.lineTo(px - s * 0.5f, py + s);
        c.drawPath(path, stroke);
        c.restore();
        if (text.isEmpty()) return;
        float pw = pillWidth(text), ph = pillHeight(), gap = 8 * dp;
        float x, y = py - ph / 2;
        if (cx < 0) x = px + s + gap;
        else if (cx > w) x = px - s - gap - pw;
        else { x = px - pw / 2; y = cy < topInset ? py + s + gap : py - s - gap - ph; }
        x = Math.max(2 * dp, Math.min(w - pw - 2 * dp, x));
        y = Math.max(topInset + 2 * dp, Math.min(bottom - ph - 2 * dp, y));
        drawPill(c, text, place, color, x, y, alpha);
    }

    /** The display's own outline, for lining it up with the camera picture on the monitor. */
    private void drawCalib(Canvas c) {
        float w = getWidth(), h = getHeight(), in = 2 * dp + 1.5f * dp;
        stroke.setColor(ACCENT); stroke.setAlpha(255); stroke.setPathEffect(null);
        stroke.setStrokeWidth(3 * dp);
        c.drawRect(in, in, w - in, h - in, stroke);
        float l = 44 * dp, o = 14 * dp;
        stroke.setStrokeWidth(6 * dp);
        path.rewind();
        path.moveTo(o, o + l); path.lineTo(o, o); path.lineTo(o + l, o);
        path.moveTo(w - o - l, o); path.lineTo(w - o, o); path.lineTo(w - o, o + l);
        path.moveTo(w - o, h - o - l); path.lineTo(w - o, h - o); path.lineTo(w - o - l, h - o);
        path.moveTo(o + l, h - o); path.lineTo(o, h - o); path.lineTo(o, h - o - l);
        c.drawPath(path, stroke);
        stroke.setStrokeWidth(2 * dp);
        float cr = 18 * dp;
        c.drawLine(w / 2 - cr, h / 2, w / 2 + cr, h / 2, stroke);
        c.drawLine(w / 2, h / 2 - cr, w / 2, h / 2 + cr, stroke);
        String msg = "Fit this frame onto the pink rectangle, then tap";
        c.drawText(msg, (w - calibText.measureText(msg)) / 2, h / 2 + cr + 30 * dp, calibText);
    }
}
