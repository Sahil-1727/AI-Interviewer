import { Room, RoomEvent, Track } from "https://cdn.jsdelivr.net/npm/livekit-client@2.15.6/+esm";

const startButton = document.querySelector("#start-button");
const muteButton = document.querySelector("#mute-button");
const statusText = document.querySelector("#status-text");
const statusLight = document.querySelector("#status-light");
const visualizer = document.querySelector("#visualizer");
const caption = document.querySelector("#visualizer-caption");
const transcript = document.querySelector("#transcript");
const elapsed = document.querySelector("#elapsed");
const toast = document.querySelector("#toast");

let room;
let connectedAt;
let timer;
let toastTimer;
let muted = false;
let conversationStarted = false;

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 4800);
}

function setStatus(message, { active = false, speaking = false } = {}) {
  statusText.textContent = message;
  statusLight.classList.toggle("connected", active);
  visualizer.classList.toggle("connected", active);
  visualizer.classList.toggle("speaking", speaking);
  caption.textContent = speaking ? "YOUR INTERVIEWER IS SPEAKING" : active ? "LISTENING, TAKE YOUR TIME" : "YOUR VOICE, WHEN YOU’RE READY";
}

function appendTranscript(text, speaker = "Interviewer") {
  const placeholder = transcript.querySelector(".transcript-placeholder");
  placeholder?.remove();
  const line = document.createElement("p");
  const label = document.createElement("strong");
  label.textContent = `${speaker}: `;
  line.append(label, document.createTextNode(text));
  transcript.append(line);
  transcript.scrollTop = transcript.scrollHeight;
}

function formatElapsed() {
  if (!connectedAt) return;
  const total = Math.floor((Date.now() - connectedAt) / 1000);
  elapsed.textContent = `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

function closeRoom() {
  clearInterval(timer);
  timer = undefined;
  connectedAt = undefined;
  room?.disconnect();
  room = undefined;
  conversationStarted = false;
  muted = false;
  visualizer.classList.remove("muted");
  muteButton.innerHTML = '<span class="small-mic">◉</span><span>Mute mic</span>';
  muteButton.disabled = true;
  startButton.disabled = false;
  startButton.innerHTML = '<span class="button-icon">↗</span><span>Begin interview</span><span class="button-shortcut">↵</span>';
  elapsed.textContent = "00:00";
  setStatus("Ready when you are");
}

async function connect() {
  startButton.disabled = true;
  startButton.innerHTML = "<span>Connecting…</span>";
  setStatus("Connecting to your interviewer");
  try {
    const detailsResponse = await fetch("/api/connection-details");
    if (!detailsResponse.ok) {
      const error = await detailsResponse.json().catch(() => ({}));
      throw new Error(error.detail || "Could not create a LiveKit session.");
    }
    const details = await detailsResponse.json();
    room = new Room({ adaptiveStream: true, dynacast: true });
    room.on(RoomEvent.Disconnected, () => {
      closeRoom();
      showToast("You’ve left the interview room.");
    });
    room.on(RoomEvent.TrackSubscribed, (track, publication, participant) => {
      if (track.kind === Track.Kind.Audio) {
        const audio = track.attach();
        audio.autoplay = true;
        document.body.append(audio);
      }
      if (participant.isAgent && track.kind === Track.Kind.Audio) setStatus("Your interviewer is speaking", { active: true, speaking: true });
    });
    room.on(RoomEvent.TrackUnsubscribed, (track) => track.detach().forEach((element) => element.remove()));
    room.on(RoomEvent.ParticipantConnected, (participant) => {
      if (participant.isAgent) setStatus("Your interviewer joined", { active: true });
    });
    room.on(RoomEvent.DataReceived, (payload, participant) => {
      try {
        const message = JSON.parse(new TextDecoder().decode(payload));
        const text = message.text || message.alternatives?.[0]?.text;
        if (text && (message.type?.includes("transcription") || message.type?.includes("transcript"))) {
          appendTranscript(text, participant?.isAgent ? "Interviewer" : "You");
        }
      } catch { /* Ignore unrelated room data messages. */ }
    });
    room.on(RoomEvent.TranscriptionReceived, (segments, participant) => {
      for (const segment of segments) {
        if (segment.final && segment.text) {
          appendTranscript(segment.text, participant?.isAgent ? "Interviewer" : "You");
        }
      }
    });
    room.on(RoomEvent.ActiveSpeakersChanged, (speakers) => {
      const agentSpeaking = speakers.some((participant) => participant.isAgent);
      setStatus(agentSpeaking ? "Your interviewer is speaking" : "Listening, take your time", { active: true, speaking: agentSpeaking });
    });
    room.on(RoomEvent.MediaDevicesError, (error) => showToast(`Microphone access failed: ${error.message}`));

    await room.connect(details.serverUrl, details.participantToken);
    await room.localParticipant.setMicrophoneEnabled(true);
    connectedAt = Date.now();
    timer = setInterval(formatElapsed, 1000);
    muteButton.disabled = false;
    startButton.innerHTML = '<span class="button-icon">×</span><span>End interview</span>';
    startButton.disabled = false;
    setStatus("Connected · waiting for your interviewer", { active: true });
    conversationStarted = true;
    startButton.innerHTML = '<span class="button-icon">×</span><span>End interview</span>';
    showToast("You’re in. Your interviewer will join shortly.");
  } catch (error) {
    console.error(error);
    closeRoom();
    showToast(error.message || "Could not connect. Check that the agent worker is running.");
  }
}

startButton.addEventListener("click", () => {
  if (room && conversationStarted) {
    closeRoom();
    showToast("Interview ended. Take a moment before your next one.");
  } else if (!room) {
    connect();
  }
});

muteButton.addEventListener("click", async () => {
  if (!room) return;
  muted = !muted;
  try {
    await room.localParticipant.setMicrophoneEnabled(!muted);
    visualizer.classList.toggle("muted", muted);
    muteButton.innerHTML = muted ? '<span class="small-mic">◉</span><span>Unmute mic</span>' : '<span class="small-mic">◉</span><span>Mute mic</span>';
    setStatus(muted ? "Microphone muted" : "Listening, take your time", { active: true });
  } catch (error) {
    muted = !muted;
    showToast(error.message || "Could not change microphone state.");
  }
});

document.querySelector("#help-button").addEventListener("click", () => {
  showToast("A private voice conversation tailored to the role and experience details in this interview.");
});

const clock = document.querySelector("#clock");
function updateClock() {
  clock.textContent = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date()) + " LOCAL";
}
updateClock();
setInterval(updateClock, 30_000);

fetch("/api/interview-config")
  .then((response) => response.ok ? response.json() : null)
  .then((config) => {
    if (!config) return;
    document.querySelector("#role-title").textContent = config.role || "Technical interview";
    document.querySelector("#role-level").textContent = config.experience_level || "Personalized to your experience";
  })
  .catch(() => {});
