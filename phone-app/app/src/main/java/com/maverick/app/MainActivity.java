package com.maverick.app;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.util.Log;
import android.view.Gravity;
import android.view.HapticFeedbackConstants;
import android.view.View;
import android.view.WindowManager;
import android.webkit.ConsoleMessage;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * Maverick on the phone: a thin shell around the hub's phone page (<hub>/app).
 * Address: intent extra "url" (saved), else the last one saved, else http://127.0.0.1:8000/app,
 * which reaches the Mac through "adb reverse tcp:8000 tcp:8000".
 * Until the page has loaded, and whenever it cannot be loaded, a plain splash shows and the app retries every 2 s.
 */
public class MainActivity extends Activity {
    private static final String TAG = "MaverickApp";
    private static final String DEFAULT_URL = "http://127.0.0.1:8000/app";
    private static final int INK = Color.rgb(5, 5, 5);
    private static final long RETRY_MS = 2000;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private WebView web;
    private View splash;
    private TextView status;
    private String url = DEFAULT_URL;
    private boolean failed;          // the main page of the load in progress did not arrive
    private boolean shown;           // the page is on screen
    private final Runnable retry = this::load;

    @Override
    @SuppressLint("SetJavaScriptEnabled")
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().setStatusBarColor(INK);
        getWindow().setNavigationBarColor(INK);
        getWindow().getDecorView().setBackgroundColor(INK);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(INK);

        web = new WebView(this);
        web.setBackgroundColor(INK);
        web.setOverScrollMode(View.OVER_SCROLL_NEVER);
        web.setAlpha(0f);
        web.setVisibility(View.INVISIBLE);
        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setMediaPlaybackRequiresUserGesture(false);
        ws.setSupportZoom(false);
        ws.setBuiltInZoomControls(false);
        ws.setDisplayZoomControls(false);
        ws.setTextZoom(100);
        web.addJavascriptInterface(new Bridge(), "MaverickApp");
        web.setWebViewClient(new WebViewClient() {
            // "failed" is cleared only when a load is started (see load): WebView reports an HTTP error
            // before it reports that the page started, so clearing it on page start would lose the error.
            @Override public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame()) { failed = true; Log.w(TAG, "page not loaded: " + err.getDescription()); }
            }
            @Override public void onReceivedHttpError(WebView v, WebResourceRequest req, WebResourceResponse res) {
                if (req.isForMainFrame() && res.getStatusCode() >= 400) { failed = true; Log.w(TAG, "page answered " + res.getStatusCode()); }
            }
            @Override public void onPageFinished(WebView v, String u) {
                if (failed) showSplash("Can't reach Maverick. Retrying…", true);
                else showPage();
            }
            @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest req) { return false; }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onConsoleMessage(ConsoleMessage m) {
                Log.i(TAG, "page: " + m.message());
                return true;
            }
        });
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));

        // Splash: the name, and one quiet line saying what is happening
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setGravity(Gravity.CENTER);
        col.setBackgroundColor(INK);
        TextView name = new TextView(this);
        name.setText("Maverick");
        name.setTextSize(28);
        name.setTextColor(Color.WHITE);
        name.setTypeface(Typeface.create(Typeface.DEFAULT, Typeface.BOLD));
        if (android.os.Build.VERSION.SDK_INT >= 28) name.setTypeface(Typeface.create(Typeface.DEFAULT, 600, false));
        name.setGravity(Gravity.CENTER);
        status = new TextView(this);
        status.setTextSize(14);
        status.setTextColor(Color.argb(153, 255, 255, 255));
        status.setGravity(Gravity.CENTER);
        status.setText("Connecting…");
        LinearLayout.LayoutParams statusLp = new LinearLayout.LayoutParams(-2, -2);
        statusLp.topMargin = Math.round(10 * getResources().getDisplayMetrics().density);
        col.addView(name, new LinearLayout.LayoutParams(-2, -2));
        col.addView(status, statusLp);
        splash = col;
        root.addView(splash, new FrameLayout.LayoutParams(-1, -1));

        setContentView(root);
        readIntent(getIntent());
        load();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        String before = url;
        readIntent(intent);
        if (!url.equals(before)) { showSplash("Connecting…", false); load(); }
    }

    private void readIntent(Intent intent) {
        SharedPreferences p = getSharedPreferences("maverick", MODE_PRIVATE);
        String given = intent == null ? null : intent.getStringExtra("url");
        if (given != null && !given.isEmpty()) p.edit().putString("url", given).apply();
        url = p.getString("url", DEFAULT_URL);
    }

    private void load() {
        ui.removeCallbacks(retry);
        failed = false;
        Log.i(TAG, "loading " + url);
        web.loadUrl(url);
    }

    private void showPage() {
        ui.removeCallbacks(retry);
        if (shown) return;
        shown = true;
        Log.i(TAG, "page loaded");
        web.setVisibility(View.VISIBLE);
        web.animate().alpha(1f).setDuration(150).withEndAction(() -> splash.setVisibility(View.GONE)).start();
    }

    private void showSplash(String line, boolean thenRetry) {
        shown = false;
        web.animate().cancel();
        web.setAlpha(0f);
        web.setVisibility(View.INVISIBLE);
        status.setText(line);
        splash.setVisibility(View.VISIBLE);
        ui.removeCallbacks(retry);
        if (thenRetry) ui.postDelayed(retry, RETRY_MS);
    }

    @Override
    public void onBackPressed() {
        if (shown && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        ui.removeCallbacks(retry);
        web.destroy();
        super.onDestroy();
    }

    /** What the page may ask of the phone: window.MaverickApp.haptic() and .version(). */
    private final class Bridge {
        @JavascriptInterface
        public void haptic() {
            ui.post(() -> {
                boolean done = web.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP);
                if (done) return;
                try {       // haptic feedback switched off in the phone's settings: a very short buzz instead
                    Vibrator v = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
                    if (v != null) v.vibrate(VibrationEffect.createOneShot(12, VibrationEffect.DEFAULT_AMPLITUDE));
                } catch (Exception e) { Log.w(TAG, "no vibration", e); }
            });
        }

        @JavascriptInterface
        public String version() {
            try { return getPackageManager().getPackageInfo(getPackageName(), 0).versionName; }
            catch (Exception e) { return ""; }
        }
    }
}
