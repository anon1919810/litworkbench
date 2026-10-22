"""One page, one process. No heavy import or model inference before parent GO."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for data in iter(lambda: handle.read(1 << 20), b""):
            digest.update(data)
    return digest.hexdigest()


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    return str(value)


def recognize(request, phase):
    import numpy as np
    import pypdfium2 as pdfium
    import onnxruntime
    from rapidocr import RapidOCR
    from rapidocr.inference_engine.onnxruntime import main as ort_main
    from omegaconf import OmegaConf
    from importlib.metadata import version

    runtime = request["runtime"]
    paths = request["models"]
    for path in paths.values():
        if not Path(path).is_file():
            raise ValueError("本地模型已丢失；不会自动下载")
    params = {f"{key}.model_path": value for key, value in paths.items()}
    params.update({"Global.log_level": "error",
                   "Rec.rec_batch_num": runtime["rec_batch"],
                   "Cls.cls_batch_num": runtime["cls_batch"],
                   "EngineConfig.onnxruntime.intra_op_num_threads": runtime["onnx_intra_threads"],
                   "EngineConfig.onnxruntime.inter_op_num_threads": runtime["onnx_inter_threads"]})
    recorded = []
    original = onnxruntime.InferenceSession
    original_main = ort_main.InferenceSession

    class Recording(original):
        def __init__(self, path, *args, **kwargs):
            recorded.append(str(path))
            super().__init__(path, *args, **kwargs)

    onnxruntime.InferenceSession = ort_main.InferenceSession = Recording
    try:
        engine = RapidOCR(params=params)
        phase()
        source = Path(request["source"])
        if sha256(source) != request["source_sha256"]:
            raise ValueError("PDF 原件指纹发生变化")
        pdf = pdfium.PdfDocument(str(source))
        try:
            page = pdf[int(request["file_page"]) - 1]
            try:
                bitmap = page.render(scale=float(runtime["render_scale"]))
                try:
                    image = bitmap.to_pil().convert("RGB")
                    try:
                        width, height = image.size
                        output = engine(np.array(image))
                    finally:
                        image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            pdf.close()
    finally:
        onnxruntime.InferenceSession = original
        ort_main.InferenceSession = original_main
    texts = [] if output.txts is None else list(output.txts)
    scores = [] if output.scores is None else list(output.scores)
    boxes = [] if output.boxes is None else list(output.boxes)
    lines = [{"text": str(text),
              "box": [[float(p[0]), float(p[1])] for p in boxes[i]] if i < len(boxes) else [],
              "confidence": float(scores[i]) if i < len(scores) else None}
             for i, text in enumerate(texts) if str(text).strip()]
    confidence = [line["confidence"] for line in lines if line["confidence"] is not None]
    fingerprints = [{"path": path, "filename": Path(path).name, "sha256": sha256(path),
                     "size_bytes": Path(path).stat().st_size, "status": "ok"}
                    for path in dict.fromkeys(recorded)]
    if not fingerprints:
        raise ValueError("未捕获实际模型路径，拒绝无来源结果")
    return {"text": "\n".join(line["text"] for line in lines), "lines": lines,
            "engine": "rapidocr", "versions": {name: version(name) for name in
                                               ("rapidocr", "onnxruntime", "pypdfium2")},
            "mean_confidence": sum(confidence) / len(confidence) if confidence else None,
            "model_fingerprints": fingerprints,
            "config": {"profile_id": request["profile_id"], "layout": "original",
                       "runtime": runtime, "engine": _jsonable(OmegaConf.to_container(engine.cfg, resolve=True))},
            "coordinate_system": "render_pixels", "render_width": width, "render_height": height,
            "source_sha256": request["source_sha256"], "file_page": request["file_page"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    if sys.stdin.readline().strip() != "GO":
        return 2
    protocol = sys.stdout

    def emit(payload):
        protocol.write(json.dumps(payload, ensure_ascii=True) + "\n")
        protocol.flush()

    try:
        with redirect_stdout(sys.stderr):
            result = recognize(json.loads(args.request), lambda: emit({"phase": "page"}))
        emit({"result": result})
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
