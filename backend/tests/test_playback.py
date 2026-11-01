"""Playback must work for completed/imported fragmented MP4 without replacing its source."""
import hashlib
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import artifacts, db
from app.main import app
from app.pipeline.audio import needs_remux
from app import playback
from app.pipeline.base import StageError


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='需要 ffmpeg')
def test_completed_fragmented_video_plays_without_replacing_source(tmp_settings, monkeypatch, tmp_path):
    from app.pipeline import runner
    monkeypatch.setattr(runner, 'enqueue', lambda _: pytest.fail('must not reprocess'))
    vid = '123abc456def'
    db.execute('INSERT INTO videos VALUES (?,?,?,?,?,?,?)',
               (vid, 'fragmented', 'source.mp4', 2.0, db.now_iso(), 'done', None))
    root = artifacts.video_dir(vid)
    root.mkdir(parents=True)
    source = root / 'source.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=navy:s=160x120:d=2',
                    '-f', 'lavfi', '-i', 'sine=duration=2', '-c:v', 'libx264', '-c:a', 'aac',
                    '-movflags', 'frag_keyframe+empty_moov', '-shortest', str(source)], check=True)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    assert needs_remux(source)
    # A Windows reader must never prevent preparation of the independent playback file.
    with source.open('rb'), TestClient(app) as client:
        url = f'/api/videos/{vid}/file'
        response = client.get(url)
        assert response.status_code == 200
        served = tmp_path / 'served.mp4'
        served.write_bytes(response.content)
        assert not needs_remux(served), 'Playback endpoint still serves fragmented MP4'
        for value, expected in [('bytes=0-1023', response.content[:1024]),
                                ('bytes=-128', response.content[-128:])]:
            part = client.get(url, headers={'Range': value})
            assert part.status_code == 206 and part.content == expected
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert db.fetch_one('SELECT status FROM videos WHERE id=?', (vid,))['status'] == 'done'

    cached = playback.playback_file(source)
    stamp = cached.stat().st_mtime_ns
    assert playback.playback_file(source) == cached and cached.stat().st_mtime_ns == stamp
    assert playback.playback_file(served) == served  # ordinary MP4 needs no extra copy

    def stream_hash(path):
        out = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0',
                              '-c', 'copy', '-f', 'streamhash', '-hash', 'sha256', '-'],
                             capture_output=True, check=True, text=True)
        return out.stdout

    assert stream_hash(source) == stream_hash(cached)  # audio AND video packets unchanged

    # A changed source version must not reuse the previous copy; concurrent calls build once.
    st = source.stat()
    os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    calls = []
    real_run = playback.run_tool

    def counted(args, what):
        if args[0] == 'ffmpeg':
            calls.append(args)
        return real_run(args, what)

    monkeypatch.setattr(playback, 'run_tool', counted)
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(playback.playback_file, [source] * 4))
    assert len(calls) == 1 and len(set(paths)) == 1 and paths[0] != cached

    # Failed preparation is visible, preserves the source, and is retryable.
    st = source.stat()
    os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    def failed(args, what):
        Path(args[-1]).write_bytes(b'partial output')
        return subprocess.CompletedProcess(args, 1, '', 'simulated remux failure')

    monkeypatch.setattr(playback, 'run_tool', failed)
    with TestClient(app) as client:
        response = client.get(url)
        assert response.status_code == 503 and '原视频已保留' in response.json()['detail']
    assert not list((root / '.playback').glob('*.tmp.mp4'))
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    monkeypatch.setattr(playback, 'run_tool', real_run)
    assert not needs_remux(playback.playback_file(source))

    # A source changed during conversion must never publish the mismatched output.
    st = source.stat()
    os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    def changed(args, what):
        result = real_run(args, what)
        if args[0] == 'ffmpeg':
            st = source.stat()
            os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        return result
    monkeypatch.setattr(playback, 'run_tool', changed)
    with pytest.raises(StageError, match='原视频发生变化'):
        playback.playback_file(source)
    assert not list((root / '.playback').glob('*.tmp.mp4'))
