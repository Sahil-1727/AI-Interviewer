
document.addEventListener("DOMContentLoaded", () => {

    const video = document.getElementById("candidate-video");

    const startButton = document.getElementById("start-camera");
    const stopButton = document.getElementById("stop-camera");
    const cameraStatus = document.getElementById("camera-status");

    let cameraStream = null;
    let cameraEpoch = 0;

    // Turn on camera
    startButton.addEventListener("click", async () => {

        if (cameraStream) return;

        if (!navigator.mediaDevices?.getUserMedia) {
            cameraStatus.textContent =
                "Camera access is not supported in this context.";
            return;
        }

        startButton.disabled = true;
        const token = ++cameraEpoch;
        cameraStatus.textContent = "Requesting camera access...";

        try {

            const requestedStream = await navigator.mediaDevices.getUserMedia({
                video: {
                    facingMode: "user"
                },
                audio: false
            });

            if (token !== cameraEpoch) {
                requestedStream.getTracks().forEach(track => track.stop());
                return;
            }
            cameraStream = requestedStream;

            video.srcObject = cameraStream;

            await video.play();
            if (token !== cameraEpoch) return;

            stopButton.disabled = false;
            cameraStatus.textContent = "Camera is on";

        } catch (error) {

            if (token !== cameraEpoch) return;

            console.error("Camera error:", error);

            if (cameraStream) {
                cameraStream.getTracks().forEach(track => track.stop());
                cameraStream = null;
            }

            video.srcObject = null;

            cameraStatus.textContent =
                error.name === "NotAllowedError"
                    ? "Camera permission denied."
                    : "Unable to start camera: " + error.message;

            startButton.disabled = false;
        }

    });

    // Turn off camera
    function stopCamera() {

        ++cameraEpoch;

        if (cameraStream) {

            cameraStream.getTracks().forEach(track => {
                track.stop();
            });

            cameraStream = null;
        }

        video.srcObject = null;

        startButton.disabled = false;
        stopButton.disabled = true;

        cameraStatus.textContent = "Camera is off";
    }

    stopButton.addEventListener("click", stopCamera);

    // Release camera when leaving the interview page
    window.addEventListener("pagehide", stopCamera);

});
