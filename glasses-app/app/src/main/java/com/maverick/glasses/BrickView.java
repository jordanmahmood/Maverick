package com.maverick.glasses;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.graphics.Typeface;
import android.os.SystemClock;
import android.text.TextPaint;
import android.text.TextUtils;
import android.view.View;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * Draws up to two LEGO parts as slowly rotating 3D bricks with plain Canvas (no OpenGL).
 * Orthographic projection, fixed downward tilt, back-face culling (all bodies are convex),
 * studs drawn far-to-near on top. Units: 1 = one stud pitch, a plate is 0.4 high.
 */
public class BrickView extends View {
    private static final float TILT = (float) Math.toRadians(25);
    private static final float SIN_T = (float) Math.sin(TILT), COS_T = (float) Math.cos(TILT);
    private static final float DEG_PER_SEC = 40f;
    private static final float STUD_R = 0.3f, STUD_H = 0.21f, PLATE = 0.4f;
    private static final float[] LIGHT = norm(new float[]{-0.35f, 0.75f, 0.55f});

    static class Brick {
        int w = 2, l = 4, h = 3, count = 1, color = Color.GRAY;
        boolean studs = true;
        String shape = "box", label = "";
    }

    private final List<Brick> bricks = new ArrayList<>();
    private final Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint line = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final TextPaint text = new TextPaint(Paint.ANTI_ALIAS_FLAG);
    private final Path path = new Path();
    private final RectF oval = new RectF();
    private boolean running = true;

    public BrickView(Context c) {
        super(c);
        fill.setStyle(Paint.Style.FILL);
        line.setStyle(Paint.Style.STROKE);
        line.setStrokeWidth(2f * getResources().getDisplayMetrics().density);
        line.setStrokeJoin(Paint.Join.ROUND);
        text.setColor(Color.WHITE);
        text.setTextSize(15f * getResources().getDisplayMetrics().scaledDensity);
        text.setTypeface(Typeface.DEFAULT_BOLD);
        text.setTextAlign(Paint.Align.CENTER);
    }

    /** Returns the number of bricks now shown. */
    public int setBricks(JSONArray arr) {
        bricks.clear();
        if (arr != null) {
            for (int i = 0; i < arr.length() && bricks.size() < 2; i++) {
                JSONObject o = arr.optJSONObject(i);
                if (o == null) continue;
                Brick b = new Brick();
                b.w = clamp(o.optInt("w", 2), 1, 16);
                b.l = clamp(o.optInt("l", 4), 1, 16);
                if (b.w > b.l) { int t = b.w; b.w = b.l; b.l = t; }
                b.h = clamp(o.optInt("h", 3), 1, 18);
                b.studs = o.optBoolean("studs", true);
                b.shape = o.optString("shape", "box");
                b.count = Math.max(1, o.optInt("count", 1));
                b.label = o.optString("label", "");
                try { b.color = Color.parseColor(o.optString("color", "#9BA19D")); } catch (Exception e) { b.color = Color.GRAY; }
                bricks.add(b);
            }
        }
        invalidate();
        return bricks.size();
    }

    private boolean compact;

    public void setCompact(boolean on) {
        compact = on;
        text.setTextAlign(on ? Paint.Align.RIGHT : Paint.Align.CENTER);
        text.setTextSize((on ? 14f : 15f) * getResources().getDisplayMetrics().scaledDensity);
        invalidate();
    }

    public void setRunning(boolean r) {
        running = r;
        if (r) invalidate();
    }

    @Override
    protected void onDraw(Canvas c) {
        int n = bricks.size();
        if (n == 0) return;
        float angle = (float) Math.toRadians((SystemClock.uptimeMillis() % 9000L) / 1000f * DEG_PER_SEC);
        if (compact) {   // strip mode: first brick only, no label, "×N" in the corner
            Brick b = bricks.get(0);
            drawBrick(c, b, angle, getWidth() / 2f, 0, getWidth(), getHeight());
            if (b.count > 1) c.drawText("×" + b.count, getWidth() - 2f, getHeight() - 4f, text);
            if (running && isShown()) postInvalidateOnAnimation();
            return;
        }
        float cellH = getHeight() / (float) n;
        float labelH = text.getTextSize() * 1.5f;
        for (int i = 0; i < n; i++) {
            Brick b = bricks.get(i);
            float top = i * cellH;
            drawBrick(c, b, angle + i * 0.9f, getWidth() / 2f, top, getWidth(), cellH - labelH);
            String s = "×" + b.count + " " + b.label;
            CharSequence e = TextUtils.ellipsize(s, text, getWidth() - 8, TextUtils.TruncateAt.END);
            c.drawText(e, 0, e.length(), getWidth() / 2f, top + cellH - labelH * 0.3f, text);
        }
        if (running && isShown()) postInvalidateOnAnimation();
    }

    // ---------- one brick ----------

    private float sinA, cosA, scale, ox, oy;

    private void drawBrick(Canvas c, Brick b, float angle, float cx, float top, float availW, float availH) {
        boolean round = "round".equals(b.shape), slope = "slope".equals(b.shape);
        float L = b.l, W = round ? b.l : b.w, H = b.h * PLATE;
        boolean flatTop = !slope || b.l > 1;
        float studH = (b.studs && flatTop) ? STUD_H : 0f;
        float diag = round ? L : (float) Math.sqrt(L * L + W * W);
        float extentY = (H + studH) * COS_T + diag * SIN_T;
        scale = Math.min(availW * 0.90f / diag, availH * 0.90f / extentY);
        sinA = (float) Math.sin(angle); cosA = (float) Math.cos(angle);
        ox = cx;
        // vertical centre: the body spans y in [-H/2, H/2 + studH]
        oy = top + availH / 2f + (studH * COS_T * scale) / 2f;

        int base = liftDark(b.color);
        int edge = mix(base, Color.WHITE, 0.65f);
        line.setColor(edge);

        if (round) {
            drawCylinder(c, 0, -H / 2, 0, L / 2f, H, base);
        } else {
            List<float[][]> faces = slope ? wedgeFaces(L, W, H) : boxFaces(L, W, H);
            for (float[][] f : faces) drawFace(c, f, base);
        }

        if (studH > 0) {
            List<float[]> studs = new ArrayList<>();
            int rows = slope ? 1 : b.l;
            for (int i = 0; i < rows; i++)
                for (int j = 0; j < (round ? b.l : b.w); j++) {
                    float x = -L / 2f + 0.5f + i, z = -W / 2f + 0.5f + j;
                    if (round && b.l == 1) { x = 0; z = 0; }
                    studs.add(new float[]{x, z, -x * sinA + z * cosA});
                }
            Collections.sort(studs, (p, q) -> Float.compare(p[2], q[2]));   // far first
            for (float[] s : studs) drawCylinder(c, s[0], H / 2, s[1], STUD_R, STUD_H, base);
        }
    }

    private static List<float[][]> boxFaces(float L, float W, float H) {
        float x = L / 2, y = H / 2, z = W / 2;
        List<float[][]> f = new ArrayList<>();
        f.add(new float[][]{{-x, y, -z}, {x, y, -z}, {x, y, z}, {-x, y, z}});       // top
        f.add(new float[][]{{-x, -y, -z}, {x, -y, -z}, {x, -y, z}, {-x, -y, z}});   // bottom
        f.add(new float[][]{{-x, -y, z}, {x, -y, z}, {x, y, z}, {-x, y, z}});       // +z
        f.add(new float[][]{{-x, -y, -z}, {x, -y, -z}, {x, y, -z}, {-x, y, -z}});   // -z
        f.add(new float[][]{{x, -y, -z}, {x, -y, z}, {x, y, z}, {x, y, -z}});       // +x
        f.add(new float[][]{{-x, -y, -z}, {-x, -y, z}, {-x, y, z}, {-x, y, -z}});   // -x
        return f;
    }

    /** Slope brick: one flat stud row at the back (x = -L/2), then a slant down to a low lip at +L/2. */
    private static List<float[][]> wedgeFaces(float L, float W, float H) {
        float x = L / 2, y = H / 2, z = W / 2, lip = Math.min(0.2f, H * 0.3f);
        float[][] prof = L > 1
                ? new float[][]{{-x, -y}, {x, -y}, {x, -y + lip}, {-x + 1, y}, {-x, y}}
                : new float[][]{{-x, -y}, {x, -y}, {x, -y + lip}, {-x, y}};
        List<float[][]> f = new ArrayList<>();
        float[][] a = new float[prof.length][], b = new float[prof.length][];
        for (int i = 0; i < prof.length; i++) {
            a[i] = new float[]{prof[i][0], prof[i][1], z};
            b[i] = new float[]{prof[i][0], prof[i][1], -z};
        }
        f.add(a); f.add(b);
        for (int i = 0; i < prof.length; i++) {
            int j = (i + 1) % prof.length;
            f.add(new float[][]{a[i], a[j], b[j], b[i]});
        }
        return f;
    }

    /** Fills and outlines one convex-body face if it faces the camera. The body is centred on the origin. */
    private void drawFace(Canvas c, float[][] f, int base) {
        float[][] r = new float[f.length][];
        float mx = 0, my = 0, mz = 0;
        for (int i = 0; i < f.length; i++) {
            r[i] = rot(f[i][0], f[i][1], f[i][2]);
            mx += r[i][0]; my += r[i][1]; mz += r[i][2];
        }
        float[] u = sub(r[1], r[0]), v = sub(r[2], r[1]);
        float[] nrm = norm(new float[]{u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]});
        if (nrm[0] * mx + nrm[1] * my + nrm[2] * mz < 0) { nrm[0] = -nrm[0]; nrm[1] = -nrm[1]; nrm[2] = -nrm[2]; }
        if (nrm[2] <= 0.01f) return;   // faces away
        path.rewind();
        for (int i = 0; i < r.length; i++) {
            float sx = ox + r[i][0] * scale, sy = oy - r[i][1] * scale;
            if (i == 0) path.moveTo(sx, sy); else path.lineTo(sx, sy);
        }
        path.close();
        fill.setColor(shade(base, nrm));
        c.drawPath(path, fill);
        c.drawPath(path, line);
    }

    /** Upright cylinder with its bottom centre at (x, y, z) in model space. Used for studs and round parts. */
    private void drawCylinder(Canvas c, float x, float y, float z, float radius, float height, int base) {
        float[] p = rot(x, y, z);
        float sx = ox + p[0] * scale, syB = oy - p[1] * scale, syT = syB - height * COS_T * scale;
        float rx = radius * scale, ry = radius * scale * SIN_T;
        path.rewind();
        path.moveTo(sx - rx, syT);
        path.lineTo(sx - rx, syB);
        oval.set(sx - rx, syB - ry, sx + rx, syB + ry);
        path.arcTo(oval, 180, -180);
        path.lineTo(sx + rx, syT);
        oval.set(sx - rx, syT - ry, sx + rx, syT + ry);
        path.arcTo(oval, 0, 180);
        path.close();
        fill.setColor(scaleColor(base, 0.62f));
        c.drawPath(path, fill);
        c.drawPath(path, line);
        fill.setColor(scaleColor(base, 1.0f));
        c.drawOval(oval, fill);
        c.drawOval(oval, line);
    }

    private float[] rot(float x, float y, float z) {
        float x1 = x * cosA + z * sinA, z1 = -x * sinA + z * cosA;
        return new float[]{x1, y * COS_T - z1 * SIN_T, y * SIN_T + z1 * COS_T};
    }

    // ---------- colour helpers ----------

    private static int shade(int base, float[] n) {
        float d = Math.max(0f, n[0] * LIGHT[0] + n[1] * LIGHT[1] + n[2] * LIGHT[2]);
        return scaleColor(base, 0.42f + 0.58f * d);
    }

    private static int scaleColor(int c, float k) {
        return Color.rgb(Math.min(255, Math.round(Color.red(c) * k)), Math.min(255, Math.round(Color.green(c) * k)),
                Math.min(255, Math.round(Color.blue(c) * k)));
    }

    /** Black is transparent on the waveguide: lift very dark colours to a visible grey. */
    private static int liftDark(int c) {
        float lum = 0.299f * Color.red(c) + 0.587f * Color.green(c) + 0.114f * Color.blue(c);
        // only near-black colours: a saturated red or blue is dark by this measure but shows up fine
        if (lum >= 85f || Math.max(Color.red(c), Math.max(Color.green(c), Color.blue(c))) >= 110) return c;
        return Color.rgb(Math.max(Color.red(c), 0x5A), Math.max(Color.green(c), 0x5A), Math.max(Color.blue(c), 0x5A));
    }

    private static int mix(int a, int b, float t) {
        return Color.rgb(Math.round(Color.red(a) + (Color.red(b) - Color.red(a)) * t),
                Math.round(Color.green(a) + (Color.green(b) - Color.green(a)) * t),
                Math.round(Color.blue(a) + (Color.blue(b) - Color.blue(a)) * t));
    }

    private static float[] sub(float[] a, float[] b) { return new float[]{a[0] - b[0], a[1] - b[1], a[2] - b[2]}; }

    private static float[] norm(float[] v) {
        float m = (float) Math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
        if (m < 1e-6f) return v;
        v[0] /= m; v[1] /= m; v[2] /= m;
        return v;
    }

    private static int clamp(int v, int lo, int hi) { return Math.max(lo, Math.min(hi, v)); }
}
