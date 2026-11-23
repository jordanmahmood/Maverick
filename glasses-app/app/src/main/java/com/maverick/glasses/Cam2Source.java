package com.maverick.glasses;

import android.annotation.SuppressLint;
import android.annotation.TargetApi;
import android.content.Context;
import android.graphics.ImageFormat;
import android.graphics.SurfaceTexture;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.CaptureResult;
import android.hardware.camera2.TotalCaptureResult;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.Image;
import android.media.ImageReader;
import android.os.Handler;
import android.util.Log;
import android.util.Range;
import android.util.Size;
import android.util.SizeF;
import android.view.Surface;

import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * Phone only: the rear camera through Camera2 with a zoom ratio below 1, which switches a multi-lens phone
 * to its ultrawide lens (the old camera API cannot reach it). Shows the picture on the given preview texture
 * and hands every frame to the listener as NV21, on the camera thread, exactly like the old API's preview
 * callback. Never used on the glasses.
 */
@TargetApi(30)
final class Cam2Source {
    interface Listener {
        void onFrame(Cam2Source src, byte[] nv21);
        /** Which lens is really in use, once the first picture has arrived. */
        void onLens(Cam2Source src);
        void onFailed(Cam2Source src, String why);
    }

    private static final String TAG = "MaverickHUD";

    int width, height;                 // frames handed to the listener
    int turn;                          // degrees the picture must be turned to be upright on this screen (0 or 180 in landscape)
    float zoom;                        // zoom ratio asked of the camera
    String lens = "ultrawide";         // "ultrawide" or "main"
    float hfov, vfov;                  // field of view in use, degrees (0 = unknown)
    final List<String> sizes = new ArrayList<>();

    private final Listener listener;
    private final Handler handler;
    private CameraManager cm;
    private CameraCharacteristics chars;
    private String cameraId, widestPhysical, activePhysical;
    private float zoomFloor = 1f;
    private CameraDevice device;
    private CameraCaptureSession session;
    private ImageReader reader;
    private Surface previewSurface;
    private Range<Integer> fpsRange;
    private byte[] nv21, rowV, rowU;
    private volatile boolean closed;
    private long statAt, statFrames, statNanos, lastExposureNs, lastFrameNs;

    private Cam2Source(Listener l, Handler h) { listener = l; handler = h; }

    /** Call on the camera thread. Throws when this phone cannot do it; the caller then uses the old API at 1x. */
    @SuppressLint("MissingPermission")
    static Cam2Source open(Context ctx, Handler handler, SurfaceTexture preview, int wantW, int wantH, float wantZoom,
                           int displayDegrees, Listener listener) throws Exception {
        Cam2Source s = new Cam2Source(listener, handler);
        s.cm = (CameraManager) ctx.getSystemService(Context.CAMERA_SERVICE);
        Range<Float> range = null;
        for (String id : s.cm.getCameraIdList()) {
            CameraCharacteristics c = s.cm.getCameraCharacteristics(id);
            Integer facing = c.get(CameraCharacteristics.LENS_FACING);
            Range<Float> zr = c.get(CameraCharacteristics.CONTROL_ZOOM_RATIO_RANGE);
            if (facing == null || facing != CameraCharacteristics.LENS_FACING_BACK || zr == null) continue;
            if (range == null || zr.getLower() < range.getLower()) { s.cameraId = id; s.chars = c; range = zr; }
        }
        if (s.cameraId == null) throw new IllegalStateException("no rear camera reports a zoom range");
        if (range.getLower() > 0.95f) throw new IllegalStateException("rear camera " + s.cameraId + " cannot zoom below 1x (range " + range + ")");
        s.zoomFloor = range.getLower();
        s.zoom = Math.max(wantZoom, s.zoomFloor);

        StreamConfigurationMap map = s.chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
        if (map == null) throw new IllegalStateException("no stream configurations");
        Size[] yuv = map.getOutputSizes(ImageFormat.YUV_420_888);
        if (yuv == null || yuv.length == 0) throw new IllegalStateException("no YUV sizes");
        for (Size z : yuv) s.sizes.add(z.getWidth() + "x" + z.getHeight());
        Size frame = pick(yuv, wantW, wantH);
        s.width = frame.getWidth(); s.height = frame.getHeight();
        Size[] tex = map.getOutputSizes(SurfaceTexture.class);
        // the preview keeps the aspect of the frames we send, so the AR boxes line up with it
        Size shown = tex == null || tex.length == 0 ? frame : pick(tex, Math.max(s.width, 1280), Math.max(s.height, 1280 * s.height / s.width));

        Integer so = s.chars.get(CameraCharacteristics.SENSOR_ORIENTATION);
        s.turn = ((so == null ? 90 : so) - displayDegrees + 360) % 360;
        Range<Integer>[] fps = s.chars.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES);
        if (fps != null) for (Range<Integer> r : fps)
            if (r.getUpper() == 30 && (s.fpsRange == null || r.getLower() > s.fpsRange.getLower())) s.fpsRange = r;

        // which physical camera is the wide one, for the log and the "lens" we report
        float shortest = Float.MAX_VALUE;
        for (String pid : s.chars.getPhysicalCameraIds()) {
            try {
                float[] f = s.cm.getCameraCharacteristics(pid).get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS);
                if (f != null && f.length > 0 && f[0] < shortest) { shortest = f[0]; s.widestPhysical = pid; }
            } catch (Exception e) { Log.w(TAG, "camera2: physical camera " + pid + " unreadable"); }
        }
        Log.i(TAG, "camera2: rear camera " + s.cameraId + " zoom range " + range + ", asking " + s.zoom + "x, frames " + s.width + "x" + s.height
                + ", preview " + shown.getWidth() + "x" + shown.getHeight() + ", sensor turned " + so + ", fps " + s.fpsRange
                + ", physical cameras " + s.chars.getPhysicalCameraIds() + " (widest " + s.widestPhysical + ")");

        preview.setDefaultBufferSize(shown.getWidth(), shown.getHeight());
        s.previewSurface = new Surface(preview);
        s.reader = ImageReader.newInstance(s.width, s.height, ImageFormat.YUV_420_888, 3);
        s.reader.setOnImageAvailableListener(s::onImage, handler);
        s.nv21 = new byte[s.width * s.height * 3 / 2];
        try { s.cm.openCamera(s.cameraId, s.deviceCallback, handler); }
        catch (Exception e) { s.close(); throw e; }
        return s;
    }

    /** Closest size, preferring the same aspect ratio (same rule as the old-API path). */
    private static Size pick(Size[] sizes, int w, int h) {
        Size best = sizes[0];
        double bestScore = Double.MAX_VALUE, aspect = w / (double) h;
        for (Size z : sizes) {
            double score = Math.abs(z.getWidth() * (double) z.getHeight() - w * (double) h)
                    + (Math.abs(z.getWidth() / (double) z.getHeight() - aspect) > 0.03 ? 1e7 : 0);
            if (score < bestScore) { bestScore = score; best = z; }
        }
        return best;
    }

    /** The frame size open() would choose now; lets the caller see whether a new config needs a restart. */
    boolean wouldPickSame(int wantW, int wantH) {
        try {
            StreamConfigurationMap map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
            Size z = pick(map.getOutputSizes(ImageFormat.YUV_420_888), wantW, wantH);
            return z.getWidth() == width && z.getHeight() == height;
        } catch (Exception e) { return true; }
    }

    private final CameraDevice.StateCallback deviceCallback = new CameraDevice.StateCallback() {
        @Override public void onOpened(CameraDevice d) {
            if (closed) { d.close(); return; }
            device = d;
            try {
                d.createCaptureSession(Arrays.asList(previewSurface, reader.getSurface()), sessionCallback, handler);
            } catch (Exception e) { fail("session not created: " + e); }
        }
        @Override public void onDisconnected(CameraDevice d) { fail("camera disconnected"); }
        @Override public void onError(CameraDevice d, int error) { fail("camera error " + error); }
    };

    private final CameraCaptureSession.StateCallback sessionCallback = new CameraCaptureSession.StateCallback() {
        @Override public void onConfigured(CameraCaptureSession cs) {
            if (closed) return;
            session = cs;
            try {
                CaptureRequest.Builder b = device.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
                b.addTarget(previewSurface);
                b.addTarget(reader.getSurface());
                b.set(CaptureRequest.CONTROL_ZOOM_RATIO, zoom);
                b.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO);
                if (fpsRange != null) b.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, fpsRange);
                cs.setRepeatingRequest(b.build(), captureCallback, handler);
            } catch (Exception e) { fail("capture not started: " + e); }
        }
        @Override public void onConfigureFailed(CameraCaptureSession cs) { fail("session configuration failed"); }
    };

    private final CameraCaptureSession.CaptureCallback captureCallback = new CameraCaptureSession.CaptureCallback() {
        @Override public void onCaptureCompleted(CameraCaptureSession cs, CaptureRequest req, TotalCaptureResult res) {
            if (closed) return;
            Long exp = res.get(CaptureResult.SENSOR_EXPOSURE_TIME), dur = res.get(CaptureResult.SENSOR_FRAME_DURATION);
            if (exp != null) lastExposureNs = exp;
            if (dur != null) lastFrameNs = dur;
            String pid = res.get(CaptureResult.LOGICAL_MULTI_CAMERA_ACTIVE_PHYSICAL_ID);
            if (pid == null) pid = cameraId;
            if (pid.equals(activePhysical)) return;
            activePhysical = pid;
            Float z = res.get(CaptureResult.CONTROL_ZOOM_RATIO);
            if (z != null) zoom = z;
            lens = widestPhysical == null || pid.equals(widestPhysical) ? "ultrawide" : "main";
            describeLens(pid);
            listener.onLens(Cam2Source.this);
        }
    };

    /** Field of view from the active lens: its focal length and sensor size, narrowed by any crop the zoom ratio adds. */
    private void describeLens(String pid) {
        try {
            CameraCharacteristics pc = cm.getCameraCharacteristics(pid);
            float[] f = pc.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS);
            SizeF sensor = pc.get(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE);
            if (f == null || f.length == 0 || sensor == null) { Log.i(TAG, "camera2: active physical camera " + pid + ", field of view unknown"); return; }
            // The wide lens covers its whole sensor at the lowest zoom ratio; a higher ratio on the same lens is a crop.
            double crop = "ultrawide".equals(lens) ? Math.max(1.0, zoom / zoomFloor) : Math.max(1.0, zoom);
            double sw = sensor.getWidth(), sh = sensor.getHeight();
            // frames are 4:3 (or whatever was asked); a wider sensor is cropped at the sides, a taller one top and bottom
            double want = width / (double) height;
            if (sw / sh > want) sw = sh * want; else sh = sw / want;
            hfov = (float) Math.toDegrees(2 * Math.atan(sw / (2 * f[0] * crop)));
            vfov = (float) Math.toDegrees(2 * Math.atan(sh / (2 * f[0] * crop)));
            Log.i(TAG, String.format("camera2: active physical camera %s (%s lens), focal length %.2f mm, sensor %.2f x %.2f mm, zoom ratio %.2f, field of view about %.0f x %.0f degrees",
                    pid, lens, f[0], sensor.getWidth(), sensor.getHeight(), zoom, hfov, vfov));
        } catch (Exception e) { Log.w(TAG, "camera2: lens details unreadable: " + e); }
    }

    private void onImage(ImageReader r) {
        Image img = null;
        try {
            img = r.acquireLatestImage();
            if (img == null || closed) return;
            long t0 = System.nanoTime();
            toNv21(img);
            statNanos += System.nanoTime() - t0;
            statFrames++;
            long now = android.os.SystemClock.elapsedRealtime();
            if (statAt == 0) statAt = now;
            if (now - statAt >= 5000) {
                Log.i(TAG, String.format("camera2: %.1f frames/s from the camera, %.2f ms to copy each; sensor exposure %.1f ms, frame time %.1f ms",
                        statFrames * 1000f / (now - statAt), statNanos / 1e6f / statFrames, lastExposureNs / 1e6f, lastFrameNs / 1e6f));
                statAt = now; statFrames = 0; statNanos = 0;
            }
            listener.onFrame(this, nv21);
        } catch (Exception e) {
            Log.w(TAG, "camera2: frame failed", e);
        } finally {
            if (img != null) img.close();
        }
    }

    /** YUV_420_888 (any row or pixel stride) to NV21: the luma plane, then V and U interleaved. */
    private void toNv21(Image img) {
        Image.Plane[] p = img.getPlanes();
        int w = width, h = height;
        ByteBuffer yb = p[0].getBuffer();
        int ys = p[0].getRowStride();
        if (ys == w) {
            yb.position(0);
            yb.get(nv21, 0, Math.min(w * h, yb.remaining()));
        } else {
            for (int row = 0; row < h; row++) { yb.position(row * ys); yb.get(nv21, row * w, Math.min(w, yb.remaining())); }
        }
        ByteBuffer ub = p[1].getBuffer(), vb = p[2].getBuffer();
        int us = p[1].getRowStride(), ups = p[1].getPixelStride(), vs = p[2].getRowStride(), vps = p[2].getPixelStride();
        int cw = w / 2, chh = h / 2, o = w * h;
        int needV = (cw - 1) * vps + 1, needU = (cw - 1) * ups + 1;
        if (rowV == null || rowV.length < needV) rowV = new byte[needV];
        if (rowU == null || rowU.length < needU) rowU = new byte[needU];
        for (int row = 0; row < chh; row++) {
            vb.position(row * vs); vb.get(rowV, 0, Math.min(needV, vb.remaining()));
            ub.position(row * us); ub.get(rowU, 0, Math.min(needU, ub.remaining()));
            for (int col = 0, iv = 0, iu = 0; col < cw; col++, iv += vps, iu += ups) {
                nv21[o++] = rowV[iv];
                nv21[o++] = rowU[iu];
            }
        }
    }

    private void fail(String why) {
        if (closed) return;
        Log.w(TAG, "camera2: " + why);
        listener.onFailed(this, why);
    }

    void close() {
        closed = true;
        try { if (session != null) session.close(); } catch (Exception ignored) { }
        try { if (device != null) device.close(); } catch (Exception ignored) { }
        try { if (reader != null) reader.close(); } catch (Exception ignored) { }
        try { if (previewSurface != null) previewSurface.release(); } catch (Exception ignored) { }
        session = null; device = null; reader = null; previewSurface = null;
    }
}
