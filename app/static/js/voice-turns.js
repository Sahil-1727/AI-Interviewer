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
            this.confirmedAt = null;
        }

        keepThinking(now) {
            this.lastActivity = now;
            this.started = now;
            this.prompted = false;
            this.confirmedAt = null;
        }

        sample(level, now) {
            const c = this.config;
            if (level >= c.AUDIO_LEVEL_THRESHOLD) {
                if (this.activitySince === null) this.activitySince = now;
                // Even a short sound cancels an imminent automatic finish.
                this.lastActivity = now;
                this.confirmedAt = null;
                this.prompted = false;
                if (now - this.activitySince >= c.MIN_ACTIVITY_SECONDS * 1000) {
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
            if (silence >= c.LONG_PAUSE_CONFIRMATION_SECONDS * 1000) {
                if (this.confirmedAt === null) {
                    this.confirmedAt = now;
                    return "confirmation";
                }
                if (now - this.confirmedAt >= c.TURN_CONFIRMATION_GRACE_SECONDS * 1000) return "finish";
            }
            if (!this.checkpointed && silence >= c.MID_ANSWER_PAUSE_SECONDS * 1000) {
                this.checkpointed = true;
                return "checkpoint";
            }
            return null;
        }
    }

    if (typeof module !== "undefined" && module.exports) module.exports = {TurnDetector};
    else root.VoiceTurns = {TurnDetector};
})(typeof window === "undefined" ? globalThis : window);
