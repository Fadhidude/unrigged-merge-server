import os, base64, subprocess, tempfile, shutil
from flask import Flask, request, jsonify
from pathlib import Path

app = Flask(__name__)
SESSIONS = Path('/tmp/unrigged_sessions')
SESSIONS.mkdir(exist_ok=True)

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'Unrigged Merge v4 (file-based)'})

@app.route('/add-chunk', methods=['POST'])
def add_chunk():
    sid = request.args.get('session', 'default')
    sdir = SESSIONS / sid
    sdir.mkdir(exist_ok=True)
    n = len(list(sdir.glob('chunk_*.mp3')))

    file_bytes = None
    if request.files:
        file_bytes = next(iter(request.files.values())).read()
    elif request.data:
        file_bytes = request.data

    if not file_bytes:
        return jsonify({'error': 'No file data', 'fields': list(request.files.keys())}), 400

    (sdir / f'chunk_{n:02d}.mp3').write_bytes(file_bytes)
    return jsonify({'ok': True, 'session': sid, 'chunks': n + 1})

@app.route('/merge', methods=['POST', 'GET'])
def merge():
    sid = request.args.get('session', 'default')
    sdir = SESSIONS / sid
    chunks = sorted(sdir.glob('chunk_*.mp3')) if sdir.exists() else []

    if not chunks:
        sessions_info = [str(p.name) for p in SESSIONS.iterdir()] if SESSIONS.exists() else []
        return jsonify({'error': f'No chunks for session: {sid}',
                        'active_sessions': sessions_info}), 400

    if len(chunks) == 1:
        merged = base64.b64encode(chunks[0].read_bytes()).decode()
        shutil.rmtree(sdir, ignore_errors=True)
        return jsonify({'merged': merged, 'size_mb': round(len(merged)*3/4/1024/1024, 2), 'chunks_merged': 1})

    with tempfile.TemporaryDirectory() as tmp:
        lst = os.path.join(tmp, 'list.txt')
        open(lst, 'w').write('\n'.join(f"file '{c}'" for c in chunks))
        out = os.path.join(tmp, 'merged.mp3')
        r = subprocess.run(['ffmpeg','-y','-f','concat','-safe','0','-i',lst,'-c','copy',out],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            return jsonify({'error': r.stderr[-600:]}), 500
        merged = base64.b64encode(open(out,'rb').read()).decode()
        size_mb = round(os.path.getsize(out)/1024/1024, 2)

    shutil.rmtree(sdir, ignore_errors=True)
    return jsonify({'merged': merged, 'size_mb': size_mb, 'chunks_merged': len(chunks)})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
