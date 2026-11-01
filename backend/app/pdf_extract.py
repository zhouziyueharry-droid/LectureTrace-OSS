"""Bounded, disposable PDF text worker. Never execute actions or attachments."""
import json
import sys
from pathlib import Path

from pypdf import PdfReader, overwrite_configuration


def extract(source, limits):
    overwrite_configuration(zlib_maximum_output_length=limits['max_stream_bytes'],
                            lzw_maximum_output_length=limits['max_stream_bytes'],
                            run_length_maximum_output_length=limits['max_stream_bytes'],
                            array_based_stream_maximum_output_length=limits['max_stream_bytes'],
                            maximum_declared_stream_length=limits['max_stream_bytes'],
                            page_tree_maximum_entries=limits['max_pages'] * 4,
                            jbig2dec_binary=None)
    with open(source, 'rb') as handle:
        if not handle.read(8).startswith(b'%PDF-'):
            raise ValueError('文件不是 PDF')
        handle.seek(0)
        reader = PdfReader(handle, strict=True)
        if reader.is_encrypted:
            raise ValueError('请先解密 PDF，再导入')
        if not 0 < len(reader.pages) <= limits['max_pages']:
            raise ValueError('PDF 页数超过上限或没有页面')
        texts, size = [], 0
        for page in reader.pages:
            stream = page.get_contents()
            if stream and len(stream.get_data()) > limits['max_stream_bytes']:
                raise ValueError('PDF 页面内容过大')
            text = (page.extract_text() or '').strip()
            size += len(text.encode('utf-8'))
            if size > limits['max_text_bytes']:
                raise ValueError('PDF 文字超过上限')
            texts.append(text)
        return texts


if __name__ == '__main__':
    try:
        result = {'pages': extract(sys.argv[1], json.loads(sys.argv[3]))}
    except Exception as exc:
        result = {'error': str(exc) if isinstance(exc, ValueError) else 'PDF 损坏或格式不受支持'}
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
