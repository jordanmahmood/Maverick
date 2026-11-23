package com.maverick.glasses;

import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.SystemClock;
import android.util.Log;

import java.util.ArrayDeque;

/**
 * Listens on the glasses microphone (16 kHz mono PCM16) and cuts the stream into utterances with a
 * simple energy detector tuned for a loud room: speech starts when the level stays above 1.8x the room's
 * level for 100 ms and ends after 500 ms back under that, or 500 ms far below the level of the speech itself
 * (the wearer stopped but the room is still loud). Each utterance (300 ms to 6 s, with 250 ms of pre-roll)
 * goes to the listener.
 */
class VoiceCapture {
    interface Listener {
        /** ptt = the wearer asked to be heard (slid the touchpad), as opposed to hands-free listening. */
        void onUtterance(byte[] pcm16le, int rate, boolean ptt);
        /** Push-to-talk ended without anything to send. */
        void onPttCancelled(String reason);
        void onCapturing(boolean capturing);
        /** True while the glasses are playing sound, so we do not hear ourselves. */
        boolean muted();
    }

    private static final String TAG = "MaverickHUD";
    static final int RATE = 16000;
    private static final int FRAME = 320;                 // 20 ms
    private static final int START_FRAMES = 5;            // 100 ms above threshold
    private static final int END_FRAMES = 25;             // 500 ms of quiet (or of only room noise) ends it
    private static final double START_FACTOR = 1.8;       // speech must be this far above the room
    private static final double DROP_FACTOR = 0.45;       // below this share of the speech level = the wearer stopped
    private static final double NOISE_FLOOR_CAP = 1500;   // steady noise may raise the floor this far, no further
    private static final int MIN_VOICED = 15;             // 300 ms
    private static final int MAX_FRAMES = 300;            // 6 s
    private static final int PREROLL_FRAMES = 13 + START_FRAMES;   // 250 ms before the start was detected
    private static final int KEEP_TAIL_FRAMES = 10;       // keep 200 ms of the closing quiet
    private static final double MIN_THRESHOLD = 350;

    private final Listener listener;
    private volatile boolean running;
    private Thread thread;

    VoiceCapture(Listener l) { listener = l; }

    // Push-to-talk: the wearer slides the touchpad, speaks, and it stops by itself.
    private static final int PTT_MIN_VOICED = 15;         // 300 ms of speech before the end test applies
    private static final int PTT_END_FRAMES = 35;         // 700 ms well below the speech level ends it
    private static final int PTT_NO_SPEECH_FRAMES = 150;  // nothing said in 3 s: give up
    private static final double PTT_VOICED_FACTOR = 1.3;  // louder than the room by this much counts as speech
    private static final double PTT_DROP_FACTOR = 0.55;
    private volatile int pttRequest;                      // 1 = start, 2 = cancel; taken by the mic thread
    private volatile boolean pttActive;

    boolean isRunning() { return running; }
    boolean pttActive() { return pttActive; }
    void pttStart() { if (running) pttRequest = 1; }
    void pttCancel() { if (running) pttRequest = 2; }

    synchronized void start() {
        if (running) return;
        running = true;
        thread = new Thread(this::loop, "maverick-mic");
        thread.start();
    }

    synchronized void stop() {
        running = false;
        thread = null;
    }

    private void loop() {
        AudioRecord rec = null;
        try {
            int min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
            rec = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, Math.max(min, FRAME * 2 * 25));
            if (rec.getState() != AudioRecord.STATE_INITIALIZED) { Log.w(TAG, "mic not available"); return; }
            rec.startRecording();
            if (rec.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) { Log.w(TAG, "mic did not start (in use?)"); return; }
            Log.i(TAG, "mic open " + RATE + " Hz");

            ArrayDeque<short[]> pre = new ArrayDeque<>();
            short[][] utt = new short[MAX_FRAMES + PREROLL_FRAMES + 2][];
            int uttLen = 0, above = 0, quiet = 0, voiced = 0;
            boolean capturing = false, forced = false;
            int forcedFrames = 0, floorFrames = 0;
            double floor = 200, uttSum = 0, voicedSum = 0;
            long statsAt = SystemClock.elapsedRealtime();
            double statsMax = 0;

            while (running) {
                short[] buf = new short[FRAME];
                int got = 0;
                while (got < FRAME && running) {
                    int n = rec.read(buf, got, FRAME - got);
                    if (n <= 0) { Log.w(TAG, "mic read " + n); return; }
                    got += n;
                }
                double sum = 0;
                for (short s : buf) sum += (double) s * s;
                double rms = Math.sqrt(sum / FRAME);
                statsMax = Math.max(statsMax, rms);
                long now = SystemClock.elapsedRealtime();
                if (now - statsAt >= 10000) {
                    Log.i(TAG, "mic floor " + Math.round(floor) + " peak rms " + Math.round(statsMax));
                    statsAt = now; statsMax = 0;
                }

                int req = pttRequest;
                if (req != 0) {
                    pttRequest = 0;
                    if (req == 1 && !forced) {     // start now, keeping what was just heard as pre-roll
                        if (capturing) { capturing = false; listener.onCapturing(false); }
                        forced = true; pttActive = true;
                        uttLen = 0;
                        for (short[] f : pre) utt[uttLen++] = f;
                        pre.clear();
                        forcedFrames = 0; voiced = 0; voicedSum = 0; quiet = 0; uttSum = 0; above = 0;
                        Log.i(TAG, "ptt start");
                    } else if (req == 2 && forced) {
                        forced = false; pttActive = false;
                        uttLen = 0; voiced = 0; voicedSum = 0; quiet = 0; above = 0;
                        Log.i(TAG, "ptt cancelled (slide)");
                        listener.onPttCancelled("slide");
                    }
                }
                if (forced) {
                    // Forced listening: no start test, the room level is not re-learned, and it ends when the
                    // level stays far below the wearer's own speech, whatever the room is doing.
                    utt[uttLen++] = buf;
                    forcedFrames++;
                    uttSum += rms;
                    double room = floorFrames >= 25 ? floor : 350;
                    if (rms > PTT_VOICED_FACTOR * room) { voiced++; voicedSum += rms; }
                    if (voiced >= PTT_MIN_VOICED) quiet = rms < PTT_DROP_FACTOR * (voicedSum / voiced) ? quiet + 1 : 0;
                    boolean hardStop = forcedFrames >= MAX_FRAMES;
                    if (voiced == 0 && forcedFrames >= PTT_NO_SPEECH_FRAMES) {
                        forced = false; pttActive = false;
                        Log.i(TAG, "ptt cancelled (no speech)");
                        listener.onPttCancelled("no speech");
                        uttLen = 0; voiced = 0; voicedSum = 0; quiet = 0;
                    } else if (quiet >= PTT_END_FRAMES || hardStop) {
                        forced = false; pttActive = false;
                        int frames = uttLen - Math.max(0, quiet - KEEP_TAIL_FRAMES);
                        byte[] pcm = new byte[frames * FRAME * 2];
                        int o = 0;
                        for (int i = 0; i < frames; i++)
                            for (short s : utt[i]) { pcm[o++] = (byte) (s & 0xff); pcm[o++] = (byte) ((s >> 8) & 0xff); }
                        Log.i(TAG, "ptt sent " + (frames * 20) + " ms, mean rms " + Math.round(uttSum / Math.max(1, forcedFrames))
                                + (hardStop ? " (6 s limit)" : ""));
                        listener.onUtterance(pcm, RATE, true);
                        uttLen = 0; voiced = 0; voicedSum = 0; quiet = 0;
                    }
                    continue;
                }

                if (listener.muted()) {            // our own speaker is on: drop everything
                    if (capturing) { capturing = false; listener.onCapturing(false); }
                    pre.clear(); uttLen = 0; above = 0; quiet = 0; voiced = 0; voicedSum = 0;
                    continue;
                }

                // The wearer's mouth is a hand's width from the microphone, so their speech stands well above
                // even a loud room. The room's level (floor) is only learned between utterances, never during one.
                double threshold = Math.max(floor * START_FACTOR, MIN_THRESHOLD);
                boolean loud = rms > threshold;
                if (!capturing) {
                    floor = rms < floor ? floor * 0.9 + rms * 0.1 : floor * 0.98 + rms * 0.02;   // follows room noise within ~1 s
                    floorFrames++;
                    pre.addLast(buf);
                    while (pre.size() > PREROLL_FRAMES) pre.removeFirst();
                    above = loud ? above + 1 : 0;
                    if (above >= START_FRAMES) {
                        capturing = true;
                        uttLen = 0;
                        for (short[] f : pre) utt[uttLen++] = f;
                        pre.clear();
                        voiced = above; quiet = 0; above = 0; uttSum = 0;
                        voicedSum = rms * voiced;
                        listener.onCapturing(true);
                    }
                } else {
                    utt[uttLen++] = buf;
                    uttSum += rms;
                    // "Stopped talking" = back under the threshold, or far below the level of the speech so far.
                    // The second test ends the utterance when the wearer stops even though the room stays loud.
                    boolean low = !loud || rms < DROP_FACTOR * (voicedSum / Math.max(1, voiced));
                    if (low) quiet++;
                    else { voiced++; voicedSum += rms; quiet = 0; }
                    boolean tooLong = uttLen >= MAX_FRAMES + PREROLL_FRAMES;
                    if (quiet >= END_FRAMES || tooLong) {
                        capturing = false;
                        listener.onCapturing(false);
                        int frames = uttLen - Math.max(0, quiet - KEEP_TAIL_FRAMES);
                        long meanRms = Math.round(uttSum / Math.max(1, uttLen - PREROLL_FRAMES));
                        // Loud for the whole 6 s with no pauses: that is room noise, not a sentence. Drop it and raise the floor.
                        boolean noise = tooLong && voiced >= uttLen * 0.95;
                        if (noise) {
                            double raised = Math.min(NOISE_FLOOR_CAP, meanRms * 0.6);
                            if (raised > floor) floor = raised;
                            Log.i(TAG, "utterance dropped: 6 s of steady noise, mean rms " + meanRms + ", floor now " + Math.round(floor));
                        } else if (voiced < MIN_VOICED) {
                            Log.i(TAG, "utterance dropped: only " + (voiced * 20) + " ms of speech, mean rms " + meanRms + ", floor " + Math.round(floor));
                        } else {
                            byte[] pcm = new byte[frames * FRAME * 2];
                            int o = 0;
                            for (int i = 0; i < frames; i++)
                                for (short s : utt[i]) { pcm[o++] = (byte) (s & 0xff); pcm[o++] = (byte) ((s >> 8) & 0xff); }
                            Log.i(TAG, "utterance sent " + (frames * 20) + " ms, mean rms " + meanRms + ", floor " + Math.round(floor));
                            listener.onUtterance(pcm, RATE, false);
                        }
                        uttLen = 0; quiet = 0; voiced = 0; above = 0; voicedSum = 0;
                    }
                }
            }
        } catch (Exception e) {
            Log.w(TAG, "mic failed", e);
        } finally {
            try { listener.onCapturing(false); } catch (Exception ignored) { }
            if (pttActive) { pttActive = false; try { listener.onPttCancelled("microphone stopped"); } catch (Exception ignored) { } }
            if (rec != null) {
                try { rec.stop(); } catch (Exception ignored) { }
                rec.release();
            }
        }
    }
}
