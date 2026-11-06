// Kecilkan foto di browser sebelum diunggah. Server tetap memvalidasi ulang.
async function compressImage(input) {
  const file = input.files && input.files[0];
  if (!file || !file.type.startsWith("image/") || file.size <= 1.5 * 1024 * 1024) {
    return;
  }
  try {
    const bitmap = await createImageBitmap(file);
    const maxSide = 1600;
    const scale = Math.min(1, maxSide / Math.max(bitmap.width, bitmap.height));
    const width = Math.max(1, Math.round(bitmap.width * scale));
    const height = Math.max(1, Math.round(bitmap.height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    canvas.getContext("2d").drawImage(bitmap, 0, 0, width, height);
    if (bitmap.close) bitmap.close();
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.82));
    if (!blob) return;
    const name = file.name.replace(/\.[^.]+$/, "") + ".jpg";
    const transfer = new DataTransfer();
    transfer.items.add(new File([blob], name, { type: "image/jpeg" }));
    input.files = transfer.files;
  } catch (error) {
    // Biarkan file asli jika browser tidak mendukung kompresi.
  }
}
