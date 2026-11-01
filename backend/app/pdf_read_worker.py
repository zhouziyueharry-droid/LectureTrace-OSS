"""Local-only, disposable page renderer / Docling worker. No model downloads here."""
import base64
import io
import json
import math
import os
import sys
from pathlib import Path


def deny_network(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo', 'socket.sendto', 'subprocess.Popen', 'os.system'):
        raise PermissionError('PDF worker 禁止网络访问或启动外部程序')


def render(source, page_number, crop, limits, long_edge):
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(source) as document:
        page = document[page_number - 1]
        try:
            width, height = page.get_size()
            x0, y0, x1, y1 = crop or (0, 0, 1, 1)
            if not all(math.isfinite(v) and v > 0 for v in (width, height)):
                raise ValueError('PDF 页面尺寸无效')
            clip_width, clip_height = width * (x1 - x0), height * (y1 - y0)
            scale = min(limits['max_render_scale'], long_edge / max(clip_width, clip_height))
            if math.ceil(clip_width * scale) * math.ceil(clip_height * scale) > limits['max_render_pixels']:
                raise ValueError('PDF 渲染超过像素上限')
            # PDFium crops in bottom/left page coordinates; tool coordinates are top/left.
            margins = (x0 * width, (1 - y1) * height, (1 - x1) * width, y0 * height)
            bitmap = page.render(scale=scale, crop=margins)
            try:
                return bitmap.to_pil().convert('RGB').copy()
            finally:
                bitmap.close()
        finally:
            page.close()


def process(source, job, limits):
    if job['kind'] == 'image':
        im = render(source, job['page'], job.get('crop'), limits, limits['image_long_edge'])
        out = io.BytesIO()
        im.save(out, format='JPEG', quality=88)
        if out.tell() > limits['max_image_bytes']:
            raise ValueError('PDF 图片超过大小上限，请缩小读取区域')
        return {'image': base64.b64encode(out.getvalue()).decode('ascii'),
                'width': im.width, 'height': im.height}
    return parse_page(source, job['page'], limits)


def parse_page(source, page_number, limits):
    # Avoid loading document models for genuinely blank pages.
    im = render(source, page_number, None, limits, limits['ocr_long_edge'])
    if all(lo == hi == 255 for lo, hi in im.getextrema()):
        return {'text': '', 'source': 'blank', 'has_text': False}
    models = Path(limits['docling_models_dir'])
    if not (models / 'ready.json').is_file():
        raise ValueError('本地 Docling 模型尚未准备，请运行 scripts/prepare_pdf_models.py；仍可用 view_pdf_page 看图')
    from docling.datamodel.base_models import InputFormat, DocumentStream, ConversionStatus
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(
        artifacts_path=models, enable_remote_services=False, allow_external_plugins=False,
        do_ocr=True, do_table_structure=True, do_picture_description=False,
        do_formula_enrichment=False, do_code_enrichment=False, do_picture_classification=False,
        accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=limits['worker_threads']),
        ocr_options=RapidOcrOptions(backend='onnxruntime', force_full_page_ocr=True),
    )
    converter = DocumentConverter(allowed_formats=[InputFormat.PDF], format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    # Bound Docling's own rasterization too: parse one already-sized page, never the full document.
    bounded = io.BytesIO()
    im.save(bounded, format='PDF', resolution=144)
    bounded.seek(0)
    result = converter.convert(DocumentStream(name='page.pdf', stream=bounded),
                               max_num_pages=1, max_file_size=limits['max_bytes'])
    if result.status != ConversionStatus.SUCCESS:
        raise ValueError('Docling 未完整识别此页，请使用 view_pdf_page 核对')
    text = result.document.export_to_markdown().strip()
    if len(text.encode('utf-8')) > limits['max_text_bytes']:
        raise ValueError('PDF 识别文字超过上限')
    return {'text': text, 'source': 'docling', 'has_text': bool(text)}


if __name__ == '__main__':
    source, destination = sys.argv[1:3]
    job, limits = json.loads(sys.argv[3]), json.loads(sys.argv[4])
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                      OMP_NUM_THREADS=str(limits['worker_threads']),
                      MKL_NUM_THREADS=str(limits['worker_threads']), TOKENIZERS_PARALLELISM='false')
    sys.addaudithook(deny_network)
    try:
        result = process(source, job, limits)
    except Exception as exc:
        result = {'error': str(exc)[:500] if isinstance(exc, (ValueError, ImportError, PermissionError))
                  else f'本地 PDF 处理失败（{type(exc).__name__}），可尝试读取原生文字或页面图片'}
    Path(destination).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
