/**
 * OCR 行框覆盖层。
 *
 * 核心 OCR 结构里的行坐标是 box 多边形（[[x, y] × 4]），不是 bbox；
 * 这里把多边形换算成外接矩形，并按页面渲染尺寸归一化成百分比，
 * 因此“适应宽度”和“原始大小”两种显示方式都能对齐。
 *
 * 规则：
 * - 只有每一行都带有效坐标、且 render_width/render_height 可用时才绘制；否则整体不画。
 * - 文本已被人工校订时，行框与显示文字不再一一对应，不做映射、不画框，只说明原因。
 * - 任何坐标异常（缺字段、非数字、在页面之外）都按“不可用”处理，不推测、不补齐。
 */
import type { OcrInfo, OcrLine, TextVersion } from '../api/types';

export interface OverlayBox {
  key: string;
  text: string;
  left: number;
  top: number;
  width: number;
  height: number;
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

/** 多边形 [[x, y], ...] → 外接矩形；至少 3 个有效顶点，坐标必须非负。 */
function boundsFromPolygon(value: unknown): { x: number; y: number; w: number; h: number } | null {
  if (!Array.isArray(value) || value.length < 3) return null;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const point of value) {
    if (!Array.isArray(point) || point.length < 2 || !isFiniteNumber(point[0]) || !isFiniteNumber(point[1])) return null;
    const [x, y] = point as [number, number];
    if (x < 0 || y < 0) return null;
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
  }
  const w = maxX - minX;
  const h = maxY - minY;
  if (!(w > 0) || !(h > 0)) return null;
  return { x: minX, y: minY, w, h };
}

/** 兼容个别实现给出的 bbox [x, y, w, h]。 */
function boundsFromBox(value: unknown): { x: number; y: number; w: number; h: number } | null {
  if (!Array.isArray(value) || value.length !== 4 || !value.every(isFiniteNumber)) return null;
  const [x, y, w, h] = value as [number, number, number, number];
  if (x < 0 || y < 0 || !(w > 0) || !(h > 0)) return null;
  return { x, y, w, h };
}

function lineBounds(line: OcrLine): { x: number; y: number; w: number; h: number } | null {
  return boundsFromPolygon(line.box) ?? boundsFromBox(line.bbox);
}

export interface OverlayOptions {
  /** 当前显示的是哪个文本版本；校订版不做行框映射 */
  textVersion?: TextVersion | null;
}

/**
 * 返回可直接叠加到页面图上的百分比矩形；不可靠时返回 null，界面改为文字说明。
 */
export function ocrOverlayBoxes(ocr: OcrInfo | null | undefined, options: OverlayOptions = {}): OverlayBox[] | null {
  if (!ocr) return null;
  if (options.textVersion === 'corrected') return null;

  const lines = Array.isArray(ocr.lines) ? ocr.lines : null;
  if (!lines || lines.length === 0) return null;
  const width = ocr.render_width;
  const height = ocr.render_height;
  if (!isFiniteNumber(width) || !isFiniteNumber(height) || width <= 0 || height <= 0) return null;

  const boxes: OverlayBox[] = [];
  for (const [index, line] of lines.entries()) {
    const bounds = lineBounds(line);
    // 只要有一行缺坐标就整体不画：避免只画一半却让人以为覆盖了整页。
    if (!bounds) return null;
    if (bounds.x >= width || bounds.y >= height) return null;
    const clippedW = Math.min(bounds.w, width - bounds.x);
    const clippedH = Math.min(bounds.h, height - bounds.y);
    if (!(clippedW > 0) || !(clippedH > 0)) return null;
    boxes.push({
      key: `ocr-${index}`,
      text: typeof line.text === 'string' ? line.text : '',
      left: (bounds.x / width) * 100,
      top: (bounds.y / height) * 100,
      width: (clippedW / width) * 100,
      height: (clippedH / height) * 100,
    });
  }
  return boxes.length ? boxes : null;
}