/* Audio energy is a heuristic, not speech recognition or semantic turn detection.
 * Pure timing policy, shared by the browser and simulated Node tests. */
(function (root) {
    class TurnDetector {
        constructor(config, now) {
            this.config = config;
            this.started = now;
            this.lastActivity = now;
            this.activitySince = null;
            this.hasSpeech = false;
            this.prompted = false;
            this.checkpointed = false;
        }

        keepThinking(now) {
            this.lastActivity = now;
            this.started = now;
            this.prompted = false;
        }

        waitForSpeech(now) {
            this.keepThinking(now);
            this.hasSpeech = false;
            this.activitySince = null;
            this.checkpointed = false;
        }

        sample(level, now) {
            const c = this.config;
            if (level >= c.AUDIO_LEVEL_THRESHOLD) {
                if (this.activitySince === null) this.activitySince = now;
                if (now - this.activitySince >= c.MIN_ACTIVITY_SECONDS * 1000) {
                    this.lastActivity = now;
                    this.prompted = false;
                    this.hasSpeech = true;
                    this.checkpointed = false;
                    return "activity";
                }
                return "sound";
            }
            this.activitySince = null;
            if (!this.hasSpeech) {
                if (!this.prompted && now - this.started >= c.INITIAL_THINKING_PROMPT_SECONDS * 1000) {
                    this.prompted = true;
                    return "initial-prompt";
                }
                return null; // Initial silence can NEVER finish a turn.
            }
            const silence = now - this.lastActivity;
            if (silence >= c.END_OF_ANSWER_SILENCE_SECONDS * 1000) return "finish";
            if (!this.checkpointed && silence >= c.COMMAND_CHECKPOINT_SECONDS * 1000) {
                this.checkpointed = true;
                return "checkpoint";
            }
            return null;
        }
    }

    if (typeof module !== "undefined" && module.exports) module.exports = {TurnDetector};
    else root.VoiceTurns = {TurnDetector};
})(typeof window === "undefined" ? globalThis : window);
