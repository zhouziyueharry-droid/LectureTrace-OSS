export type ViewCrop = [number, number, number, number]

export function validViewCrop(value: unknown): value is ViewCrop {
  if (!Array.isArray(value) || value.length !== 4 || !value.every((n) => typeof n === 'number' && Number.isFinite(n))) return false
  const [x, y, w, h] = value
  return x >= 0 && y >= 0 && w >= 0.05 && h >= 0.05 && x + w <= 1.000001 && y + h <= 1.000001
}

/** Normalized source-video rectangle, constrained to the player aspect ratio. */
export function viewSelection(
  start: [number, number],
  end: [number, number],
  playerAspect: number,
  sourceAspect: number,
): ViewCrop {
  const ratio = playerAspect / sourceAspect // normalized width / normalized height
  const sx = end[0] >= start[0] ? 1 : -1
  const sy = end[1] >= start[1] ? 1 : -1
  const maxW = sx > 0 ? 1 - start[0] : start[0]
  const maxH = sy > 0 ? 1 - start[1] : start[1]
  const w = Math.min(Math.max(Math.abs(end[0] - start[0]), Math.abs(end[1] - start[1]) * ratio), maxW, maxH * ratio)
  const h = w / ratio
  return [sx > 0 ? start[0] : start[0] - w, sy > 0 ? start[1] : start[1] - h, w, h]
}

export function imageBounds(playerW: number, playerH: number, videoW: number, videoH: number) {
  const scale = Math.min(playerW / videoW, playerH / videoH)
  const width = videoW * scale
  const height = videoH * scale
  return { x: (playerW - width) / 2, y: (playerH - height) / 2, width, height }
}

/** Transform a contained video element so the selected source rectangle fills its parent. */
export function viewTransform(crop: ViewCrop, playerW: number, playerH: number, videoW: number, videoH: number) {
  const image = imageBounds(playerW, playerH, videoW, videoH)
  const selectedW = crop[2] * image.width
  const selectedH = crop[3] * image.height
  const scale = Math.min(playerW / selectedW, playerH / selectedH)
  return {
    transform: `translate(${(playerW - selectedW * scale) / 2 - (image.x + crop[0] * image.width) * scale}px, ${(playerH - selectedH * scale) / 2 - (image.y + crop[1] * image.height) * scale}px) scale(${scale})`,
  }
}
