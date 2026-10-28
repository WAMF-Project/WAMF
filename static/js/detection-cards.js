/* Detection values remain data, never HTML or JavaScript source. */
(() => {
  "use strict";

  function bindImage(image) {
    image.addEventListener("load", () => checkTransparentImage(image));
    image.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (image.style.display === "none") return;
      showSnapshot(image.dataset.snapshotUrl, image.dataset.clipUrl, image.dataset.frigateEvent);
    });
    if (image.complete && image.naturalWidth > 0) checkTransparentImage(image);
  }

  function buildRow(detection) {
    const eventId = String(detection.frigate_event ?? "");
    const validEvent = typeof detection.frigate_event === "string" && eventId.length > 0
      && !/[^A-Za-z0-9_.-]/.test(eventId) && eventId !== "." && eventId !== "..";
    const mediaBase = validEvent ? `/frigate/${encodeURIComponent(eventId)}` : "";
    const card = document.createElement("div");
    card.dataset.frigateEvent = eventId;
    card.dataset.detectionTime = String(detection.detection_time ?? "");
    card.classList.add("wamf-detection-card", "new-detection");

    const image = document.createElement("img");
    image.src = validEvent ? `${mediaBase}/thumbnail.jpg` : "/static/images/1x1.png";
    image.alt = "Thumbnail";
    image.className = "wamf-detection-image";
    image.dataset.snapshotUrl = validEvent ? `${mediaBase}/snapshot.jpg` : "/static/images/1x1.png";
    image.dataset.clipUrl = validEvent ? `${mediaBase}/clip.mp4` : "";
    image.dataset.frigateEvent = eventId;
    bindImage(image);

    const content = document.createElement("div");
    content.className = "wamf-detection-content";
    const heading = document.createElement("h3");
    heading.textContent = String(detection.common_name ?? "");
    const confidence = document.createElement("div");
    confidence.className = "wamf-confidence";
    confidence.textContent = `${Math.round(detection.score * 100)}%`;
    const time = document.createElement("div");
    time.className = "wamf-detection-time";
    time.textContent = String(detection.detection_time ?? "");
    content.append(heading, confidence, time);
    card.append(image, content);
    return card;
  }

  document.querySelectorAll("img[data-snapshot-url]").forEach(bindImage);
  window.wamfDetections = { buildRow };
})();
