const state = {
  camera: {},
  scans: [],
  processing: null,
  activeScanId: null,
  loadedResultId: null,
};

const byId = (id) => document.getElementById(id);

const api = async (url, options = {}) => {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = response.status === 204 ? {} : await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed: ${response.status}`);
  return body;
};

const showMessage = (text, isError = false) => {
  const element = byId("message");
  element.textContent = text || "";
  element.classList.toggle("error", isError);
};

const setButtonState = () => {
  const cameraRunning = Boolean(state.camera.running);
  const recording = Boolean(state.camera.recording);
  const processing = state.processing?.state === "processing";
  const captured = state.activeScanId && !recording;

  byId("start-camera").disabled = cameraRunning || processing;
  byId("stop-camera").disabled = !cameraRunning || recording;
  byId("start-scan").disabled = !cameraRunning || recording || processing;
  byId("stop-scan").disabled = !recording;
  byId("process-scan").disabled = !captured || recording || processing;
};

const renderStatus = () => {
  const cameraRunning = Boolean(state.camera.running);
  const recording = Boolean(state.camera.recording);
  const usb = state.camera.device?.usb_type || "unknown";

  byId("camera-status").textContent = cameraRunning ? "READY" : "OFFLINE";
  byId("camera-detail").textContent = cameraRunning
    ? `${state.camera.device?.name || "RealSense"} · ${state.camera.resolution} @ ${state.camera.camera_fps} fps`
    : state.camera.error || "Start the camera to preview depth.";

  const usb3 = String(usb).startsWith("3");
  byId("usb-status").textContent = usb3 ? `USB ${usb}` : usb === "unknown" ? "UNKNOWN" : `USB ${usb} · SLOW`;
  byId("usb-detail").textContent = usb3
    ? "SuperSpeed link detected."
    : "Use a direct USB 3 data cable for reliable scans.";

  byId("capture-status").textContent = recording ? "RECORDING" : state.activeScanId ? "CAPTURED" : "IDLE";
  byId("capture-detail").textContent = `${state.camera.frame_count || 0} saved RGB-D frames.`;

  const job = state.processing;
  byId("process-status").textContent = job ? job.state.toUpperCase() : "WAITING";
  byId("process-detail").textContent = job?.message || "Record a full orbit first.";
  byId("process-label").textContent = job?.state === "processing" ? "WORKING" : job?.state === "error" ? "BLOCKED" : "TODO";
  byId("progress-text").textContent = job?.message || "Capture 150–300 frames.";
  byId("progress-value").style.width = `${job?.progress || 0}%`;

  byId("preview-empty").hidden = cameraRunning;
  setButtonState();
};

const renderScans = () => {
  const container = byId("scan-list");
  if (!state.scans.length) {
    container.innerHTML = '<div class="empty-row">No scans yet.</div>';
    return;
  }
  container.innerHTML = state.scans.map((scan) => `
    <div class="scan-row">
      <div><strong>${escapeHtml(scan.name)}</strong><span>${escapeHtml(scan.scan_id)}</span></div>
      <span>${scan.frame_count || 0} frames</span>
      <span class="status-tag ${scan.status === "complete" ? "medium" : ""}">${escapeHtml(scan.status.toUpperCase())}</span>
      <button class="button quiet scan-action" data-scan="${escapeHtml(scan.scan_id)}" type="button">
        ${scan.status === "complete" ? "Open result" : "Use scan"}
      </button>
    </div>
  `).join("");

  container.querySelectorAll(".scan-action").forEach((button) => {
    button.addEventListener("click", async () => {
      state.activeScanId = button.dataset.scan;
      const scan = state.scans.find((item) => item.scan_id === state.activeScanId);
      if (scan?.status === "complete") await loadResult(state.activeScanId);
      renderStatus();
    });
  });
};

const refreshStatus = async () => {
  try {
    const response = await api("/api/status");
    if (byId("message").classList.contains("error")) showMessage("");
    state.camera = response.camera || {};
    state.scans = response.scans || [];
    state.processing = response.processing;
    if (!state.activeScanId && state.camera.scan_id) state.activeScanId = state.camera.scan_id;
    if (!state.activeScanId && state.scans.length) state.activeScanId = state.scans[0].scan_id;
    renderStatus();
    renderScans();

    if (state.processing?.state === "complete" && state.processing.scan_id) {
      await loadResult(state.processing.scan_id);
    } else if (!state.processing && state.activeScanId) {
      const scan = state.scans.find((item) => item.scan_id === state.activeScanId);
      if (scan?.status === "complete" && state.loadedResultId !== state.activeScanId) {
        await loadResult(state.activeScanId);
      }
    }
  } catch (error) {
    showMessage(error.message, true);
  }
};

let previewCounter = 0;
const refreshPreview = () => {
  if (!state.camera.running) return;
  previewCounter += 1;
  byId("camera-preview").src = `/api/preview.jpg?t=${previewCounter}`;
};

const loadResult = async (scanId) => {
  try {
    const result = await api(`/api/scans/${encodeURIComponent(scanId)}/result`);
    if (result.job && result.job.state !== "complete") return;
    state.loadedResultId = scanId;
    state.activeScanId = scanId;
    byId("result-section").hidden = false;
    byId("length-mm").textContent = result.dimensions.length_mm.toFixed(1);
    byId("width-mm").textContent = result.dimensions.width_mm.toFixed(1);
    byId("height-mm").textContent = result.dimensions.height_mm.toFixed(1);
    byId("quality-label").textContent = result.quality.label;

    const artifacts = [
      ["Printable STL · millimetres", "printable-stl"],
      ["Object mesh", "object-mesh"],
      ["Object point cloud", "object-points"],
      ["Measured bounds", "object-bounds"],
      ["Raw fused scene", "raw-mesh"],
      ["Measurements JSON", "measurements"],
    ];
    byId("download-list").innerHTML = artifacts.map(([label, artifact]) => `
      <a href="/api/scans/${encodeURIComponent(scanId)}/download/${artifact}">
        <span>${label}</span><span>Download</span>
      </a>
    `).join("");

    const preview = await api(`/api/scans/${encodeURIComponent(scanId)}/preview-data`);
    pointViewer.setData(preview.points, preview.colors);
    byId("result-section").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showMessage(error.message, true);
  }
};

const escapeHtml = (value) => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

const bindActions = () => {
  byId("start-camera").addEventListener("click", async () => {
    showMessage("Opening the D435i…");
    try {
      const response = await api("/api/camera/start", { method: "POST", body: "{}" });
      state.camera = response.camera;
      showMessage("Camera ready. Frame the object and start the orbit.");
      renderStatus();
    } catch (error) {
      showMessage(error.message, true);
    }
  });

  byId("stop-camera").addEventListener("click", async () => {
    try {
      const response = await api("/api/camera/stop", { method: "POST", body: "{}" });
      state.camera = response.camera;
      renderStatus();
    } catch (error) {
      showMessage(error.message, true);
    }
  });

  byId("start-scan").addEventListener("click", async () => {
    try {
      const response = await api("/api/scan/start", {
        method: "POST",
        body: JSON.stringify({
          name: byId("scan-name").value,
          save_fps: Number(byId("save-fps").value),
          max_depth_m: Number(byId("max-depth").value),
        }),
      });
      state.camera = response.camera;
      state.activeScanId = response.camera.scan_id;
      showMessage("Recording. Move slowly around the fixed object.");
      renderStatus();
    } catch (error) {
      showMessage(error.message, true);
    }
  });

  byId("stop-scan").addEventListener("click", async () => {
    try {
      const response = await api("/api/scan/stop", { method: "POST", body: "{}" });
      state.camera = response.camera;
      state.activeScanId = response.camera.scan_id;
      showMessage("Capture saved. Review the frame count, then stitch and measure.");
      renderStatus();
      await refreshStatus();
    } catch (error) {
      showMessage(error.message, true);
    }
  });

  byId("process-scan").addEventListener("click", async () => {
    if (!state.activeScanId) return;
    showMessage("Starting offline reconstruction. The camera will stop.");
    try {
      const response = await api(`/api/scans/${encodeURIComponent(state.activeScanId)}/process`, {
        method: "POST",
        body: "{}",
      });
      state.processing = { ...response.job, scan_id: response.scan_id };
      renderStatus();
    } catch (error) {
      showMessage(error.message, true);
    }
  });

  const toggle = byId("theme-toggle");
  const syncTheme = () => {
    const dark = document.documentElement.dataset.theme === "dark";
    toggle.textContent = dark ? "Light mode" : "Dark mode";
  };
  syncTheme();
  toggle.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("codex-artifact-theme", next);
    syncTheme();
  });
};

class PointCloudViewer {
  constructor(canvas) {
    this.canvas = canvas;
    this.context = canvas.getContext("2d");
    this.points = [];
    this.colors = [];
    this.rotationX = -0.25;
    this.rotationY = 0.65;
    this.zoom = 1;
    this.dragging = false;
    this.lastX = 0;
    this.lastY = 0;

    canvas.addEventListener("pointerdown", (event) => {
      this.dragging = true;
      this.lastX = event.clientX;
      this.lastY = event.clientY;
      canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener("pointermove", (event) => {
      if (!this.dragging) return;
      this.rotationY += (event.clientX - this.lastX) * 0.008;
      this.rotationX += (event.clientY - this.lastY) * 0.008;
      this.lastX = event.clientX;
      this.lastY = event.clientY;
      this.render();
    });
    canvas.addEventListener("pointerup", () => { this.dragging = false; });
    canvas.addEventListener("wheel", (event) => {
      event.preventDefault();
      this.zoom = Math.max(0.25, Math.min(5, this.zoom * (event.deltaY > 0 ? 0.9 : 1.1)));
      this.render();
    }, { passive: false });
    window.addEventListener("resize", () => this.render());
  }

  setData(points, colors) {
    if (!points?.length) return;
    const center = [0, 1, 2].map((axis) =>
      points.reduce((sum, point) => sum + point[axis], 0) / points.length
    );
    let maxRadius = 0;
    this.points = points.map((point) => {
      const centered = [point[0] - center[0], point[1] - center[1], point[2] - center[2]];
      maxRadius = Math.max(maxRadius, Math.hypot(...centered));
      return centered;
    });
    this.points = this.points.map((point) => point.map((value) => value / Math.max(maxRadius, 1e-6)));
    this.colors = colors || [];
    this.render();
  }

  render() {
    const canvas = this.canvas;
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.floor(rect.width * dpr));
    canvas.height = Math.max(1, Math.floor(rect.height * dpr));
    const ctx = this.context;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#050505";
    ctx.fillRect(0, 0, rect.width, rect.height);
    if (!this.points.length) return;

    const cosX = Math.cos(this.rotationX);
    const sinX = Math.sin(this.rotationX);
    const cosY = Math.cos(this.rotationY);
    const sinY = Math.sin(this.rotationY);
    const projected = this.points.map((point, index) => {
      const x1 = point[0] * cosY + point[2] * sinY;
      const z1 = -point[0] * sinY + point[2] * cosY;
      const y2 = point[1] * cosX - z1 * sinX;
      const z2 = point[1] * sinX + z1 * cosX;
      const perspective = 1 / Math.max(0.45, 2.8 - z2);
      return {
        x: rect.width / 2 + x1 * rect.height * 0.95 * perspective * this.zoom,
        y: rect.height / 2 - y2 * rect.height * 0.95 * perspective * this.zoom,
        z: z2,
        color: this.colors[index] || [0.78, 0.78, 0.78],
      };
    }).sort((a, b) => a.z - b.z);

    for (const point of projected) {
      const [r, g, b] = point.color.map((value) => Math.round(value * 255));
      ctx.fillStyle = `rgb(${r},${g},${b})`;
      ctx.fillRect(point.x, point.y, 1.7, 1.7);
    }
  }
}

const pointViewer = new PointCloudViewer(byId("model-canvas"));
bindActions();
refreshStatus();
setInterval(refreshStatus, 1000);
setInterval(refreshPreview, 220);
