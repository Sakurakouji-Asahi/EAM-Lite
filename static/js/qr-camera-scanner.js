(() => {
  "use strict";

  for (const form of document.querySelectorAll("[data-qr-camera-form]")) {
    const input = form.querySelector("[data-qr-camera-value]");
    const startButton = form.querySelector("[data-qr-camera-start]");
    const stopButton = form.querySelector("[data-qr-camera-stop]");
    const video = form.querySelector("[data-qr-camera-video]");
    const fileInput = form.querySelector("[data-qr-camera-file]");
    const status = form.querySelector("[data-qr-camera-status]");
    if (!input || !startButton || !stopButton || !video || !fileInput || !status) continue;

    const Reader = window.ZXingBrowser?.BrowserQRCodeReader;
    if (!Reader) {
      status.textContent = "扫码组件未能加载，请使用扫码枪或手动输入。";
      startButton.disabled = true;
      fileInput.disabled = true;
      continue;
    }

    const scanner = new Reader();
    let controls = null;
    let starting = false;
    let submitted = false;

    const stopCamera = () => {
      if (controls) {
        try { controls.stop(); } catch (_) { /* camera may already be closed */ }
        controls = null;
      }
      const stream = video.srcObject;
      if (stream && typeof stream.getTracks === "function") {
        stream.getTracks().forEach((track) => track.stop());
      }
      video.srcObject = null;
      video.hidden = true;
      stopButton.hidden = true;
      startButton.disabled = false;
      starting = false;
    };

    const acceptResult = (result) => {
      if (submitted || !result) return;
      const value = String(result.getText?.() ?? result.text ?? "").trim();
      if (!value || value.length > 512) {
        status.textContent = "未识别出有效的资产二维码，请重新扫描。";
        return;
      }
      submitted = true;
      stopCamera();
      input.value = value;
      status.textContent = "已识别，正在核对资产。";
      form.requestSubmit();
    };

    startButton.addEventListener("click", async () => {
      if (starting || controls || submitted) return;
      if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
        status.textContent = "网页相机需要 HTTPS 或本机地址；可改用二维码照片或扫码枪。";
        return;
      }
      starting = true;
      startButton.disabled = true;
      video.hidden = false;
      status.textContent = "正在打开相机，请允许相机访问。";
      try {
        controls = await scanner.decodeFromConstraints(
          { audio: false, video: { facingMode: { ideal: "environment" } } },
          video,
          (result, _error, readerControls) => {
            if (result) {
              controls = readerControls;
              acceptResult(result);
            }
          }
        );
        if (!submitted) {
          stopButton.hidden = false;
          status.textContent = "请将二维码放入画面中央。";
        } else {
          stopCamera();
        }
      } catch (_) {
        stopCamera();
        status.textContent = "无法打开相机。请检查授权，或改用二维码照片、扫码枪。";
      } finally {
        starting = false;
      }
    });

    stopButton.addEventListener("click", () => {
      stopCamera();
      status.textContent = "相机已关闭。";
    });

    fileInput.addEventListener("change", async () => {
      const file = fileInput.files?.[0];
      if (!file || submitted) return;
      if (!file.type.startsWith("image/") || file.size > 10 * 1024 * 1024) {
        status.textContent = "请选择不超过 10 MB 的二维码照片。";
        fileInput.value = "";
        return;
      }
      stopCamera();
      status.textContent = "正在识别照片。";
      try {
        const dataUrl = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.onerror = () => reject(new Error("read failed"));
          reader.readAsDataURL(file);
        });
        acceptResult(await scanner.decodeFromImageUrl(dataUrl));
      } catch (_) {
        status.textContent = "没有在照片中找到二维码，请重拍或改用扫码枪。";
      } finally {
        fileInput.value = "";
      }
    });

    window.addEventListener("pagehide", stopCamera);
  }
})();
