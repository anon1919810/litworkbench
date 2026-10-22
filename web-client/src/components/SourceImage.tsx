import { useEffect, useMemo, useState } from 'react';
import { RENDER_SCALES, unitRenderUrl } from '../api/endpoints';
import type { RenderScale } from '../api/endpoints';
import type { UnitDetail } from '../api/types';
import { ocrOverlayBoxes } from '../lib/ocrOverlay';

interface SourceImageProps {
  unit: UnitDetail;
  scale: RenderScale;
  onScaleChange: (scale: RenderScale) => void;
  zoom: 'fit' | 'actual';
  onZoomChange: (zoom: 'fit' | 'actual') => void;
  /** 只有当前页、当前标签需要时才请求图片 */
  active: boolean;
}

/**
 * 原件视图：按所选比例（1 / 1.5 / 2 / 3）请求当前单元的 PNG。
 * 只有本地服务返回真实行坐标时才叠加 OCR 行框，否则明确说明没有可用坐标，不画假框。
 */
export function SourceImage({ unit, scale, onScaleChange, zoom, onZoomChange, active }: SourceImageProps) {
  const [failed, setFailed] = useState(false);
  const src = useMemo(() => unitRenderUrl(unit.unit_id, scale), [unit.unit_id, scale]);
  // 校订后的文字与行框不再一一对应，此时不画框，只说明原因。
  const overlayBlocked = unit.version === 'corrected';
  const boxes = useMemo(
    () => (overlayBlocked ? null : ocrOverlayBoxes(unit.ocr, { textVersion: unit.version })),
    [overlayBlocked, unit.ocr, unit.version],
  );
  const renderSize =
    unit.ocr && typeof unit.ocr.render_width === 'number' && typeof unit.ocr.render_height === 'number'
      ? `${unit.ocr.render_width} × ${unit.ocr.render_height}`
      : null;

  useEffect(() => {
    setFailed(false);
  }, [src]);

  return (
    <>
      <div className="scan-bar">
        <span>
          {unit.filename} · 比例 {scale}
          {renderSize ? ` · 渲染尺寸 ${renderSize}` : ''}
        </span>
        <span className="seg" role="group" aria-label="渲染比例">
          {RENDER_SCALES.map((value) => (
            <button key={value} type="button" aria-pressed={scale === value} onClick={() => onScaleChange(value)}>
              {value}×
            </button>
          ))}
        </span>
        <span className="seg" role="group" aria-label="显示方式">
          <button type="button" aria-pressed={zoom === 'fit'} onClick={() => onZoomChange('fit')}>
            适应宽度
          </button>
          <button type="button" aria-pressed={zoom === 'actual'} onClick={() => onZoomChange('actual')}>
            原始大小
          </button>
        </span>
      </div>
      <div className="scan-scroll">
        {active && !failed ? (
          <div className="ocr-overlay">
            <img
              className={zoom === 'actual' ? 'scan-img actual' : 'scan-img'}
              src={src}
              alt={`${unit.title || unit.filename}${unit.file_page ? `，文件第 ${unit.file_page} 页` : ''}的页面渲染`}
              decoding="async"
              onError={() => setFailed(true)}
            />
            {boxes
              ? boxes.map((box) => (
                  <span
                    key={box.key}
                    className="ocr-line"
                    title={box.text}
                    style={{
                      left: `${box.left}%`,
                      top: `${box.top}%`,
                      width: `${box.width}%`,
                      height: `${box.height}%`,
                    }}
                  />
                ))
              : null}
          </div>
        ) : (
          <div className="scan-ph" style={{ margin: 0 }}>
            <b>{failed ? '页面图未能加载' : '正在按需加载当前页'}</b>
            {failed
              ? '本地服务没有返回该单元在此比例下的页面渲染图。可改用其它比例，或对照“识别文字”核对。'
              : '只加载当前选中页的渲染图，不预先读取整本书。'}
          </div>
        )}
      </div>
      <p className="scan-note">
        {boxes
          ? `扫描区叠加了 ${boxes.length} 个真实 OCR 行框（来自本机服务返回的识别坐标），可用来核对字词位置。`
          : overlayBlocked
            ? '本页文字已被人工校订，识别行框与显示文字不再一一对应，因此不叠加任何框，也不做逐字映射。'
            : '本页没有可用的 OCR 行坐标（缺 box 或页面渲染尺寸），因此不绘制任何高亮框；定位依据只有「文件页 + 印刷页码」，请对照识别文字人工确认。'}
      </p>
    </>
  );
}