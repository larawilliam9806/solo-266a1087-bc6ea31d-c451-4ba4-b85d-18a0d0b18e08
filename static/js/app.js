(() => {
  const textarea = document.getElementById("snapshot_text");
  const fileInput = document.getElementById("snapshot_file");
  const fileDropTitle = document.querySelector(".file-drop-title");

  async function loadExample(path, button) {
    if (!textarea) return;
    button.disabled = true;
    const originalText = button.textContent;
    button.textContent = "载入中…";
    try {
      const response = await fetch(path, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      textarea.value = JSON.stringify(data, null, 2);
      textarea.focus();
    } catch (error) {
      window.alert("示例文件载入失败，请确认服务已启动。");
    } finally {
      button.disabled = false;
      button.textContent = originalText;
    }
  }

  const exampleButton = document.getElementById("load-example");
  if (exampleButton) {
    exampleButton.addEventListener("click", () => loadExample("/examples/snapshot.json", exampleButton));
  }

  const unrecoverableButton = document.getElementById("load-unrecoverable");
  if (unrecoverableButton) {
    unrecoverableButton.addEventListener("click", () =>
      loadExample("/examples/unrecoverable.json", unrecoverableButton)
    );
  }

  if (fileInput && textarea) {
    fileInput.addEventListener("change", async () => {
      const file = fileInput.files && fileInput.files[0];
      if (!file) return;
      try {
        const text = await file.text();
        textarea.value = text;
        if (fileDropTitle) fileDropTitle.textContent = `已选择：${file.name}`;
      } catch (error) {
        window.alert("无法读取该文件，请确认它是 UTF-8 编码的 JSON。");
      }
    });
  }
})();
