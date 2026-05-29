// Paprika Agent -- offscreen document (tabCapture recorder).
//
// An MV3 service worker has no DOM and can't run MediaRecorder, so tab
// audio/video capture is done here. background.js gets a media stream id
// via chrome.tabCapture.getMediaStreamId({targetTabId}) and hands it to
// this offscreen document; we open the stream with getUserMedia (the
// chromeMediaSource:"tab" constraint) and record it with MediaRecorder.
//
// Bytes egress: short clips come back inline as base64 through the same
// page<->SW relay. Large recordings (> LIMIT) only return metadata with
// too_large:true -- a direct offscreen->hub upload path is a follow-up.

const INLINE_LIMIT = 8 * 1024 * 1024; // 8 MB
let rec = null;
let chunks = [];

chrome.runtime.onMessage.addListener((m, _sender, sendResponse) => {
  if (!m || m.__offscreen !== true) return;
  (async () => {
    try {
      if (m.op === "start") {
        const c = {
          mandatory: {
            chromeMediaSource: "tab",
            chromeMediaSourceId: m.streamId,
          },
        };
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: m.audio ? c : false,
          video: m.video ? c : false,
        });
        chunks = [];
        rec = new MediaRecorder(stream, { mimeType: "video/webm" });
        rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
        rec.start(1000);
        sendResponse({ started: true });
      } else if (m.op === "stop") {
        if (!rec) { sendResponse({ error: "not recording" }); return; }
        await new Promise((res) => { rec.onstop = res; rec.stop(); });
        rec.stream.getTracks().forEach((t) => t.stop());
        const buf = await new Blob(chunks, { type: "video/webm" }).arrayBuffer();
        rec = null; chunks = [];
        if (buf.byteLength > INLINE_LIMIT) {
          sendResponse({ mime: "video/webm", bytes: buf.byteLength, too_large: true });
          return;
        }
        let bin = "";
        const u8 = new Uint8Array(buf);
        for (let i = 0; i < u8.length; i++) bin += String.fromCharCode(u8[i]);
        sendResponse({ mime: "video/webm", bytes: buf.byteLength, data_b64: btoa(bin) });
      } else {
        sendResponse({ error: "unknown offscreen op: " + m.op });
      }
    } catch (e) {
      sendResponse({ error: String((e && e.message) || e) });
    }
  })();
  return true;
});
