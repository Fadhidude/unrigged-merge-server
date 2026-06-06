from flask import Flask, request, jsonify
import base64, subprocess, tempfile, os
from collections import defaultdict

app = Flask(__name__)
sessions = defaultdict(list)

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'Unrigged Merge v2'})

@app.route('/add-chunk', methods=['POST'])
def add_chunk():
    session_id = request.args.get('session', 'default')
    if 'file' not in request.files:
        return jsonify({'error': 'No file field in form-data'}), 400
    chunk_bytes = request.files['file'].read()
    sessions[session_id].append(chunk_bytes)
    return jsonify({'ok': True, 'session': session_id, 'chunks': len(sessions[session_id])})

@app.route('/merge', methods=['POST'])
def merge():
    session_id = request.args.get('session', 'default')
    chunks_data = sessions.pop(session_id, [])
    if not chunks_data:
        return jsonify({'error': f'No chunks for session: {session_id}'}), 400
    if len(chunks_data) == 1:
        return jsonify({'merged': base64.b64encode(chunks_data[0]).decode(),
                        'size_mb': round(len(chunks_data[0])/1024/1024, 2),
                        'chunks_merged': 1})
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i, b in enumerate(chunks_data):
            p = os.path.join(tmp, f'chunk_{i:02d}.mp3')
            open(p, 'wb').write(b)
            paths.append(p)
        lst = os.path.join(tmp, 'list.txt')
        open(lst, 'w').write('\n'.join(f"file '{p}'" for p in paths))
        out = os.path.join(tmp, 'merged.mp3')
        r = subprocess.run(['ffmpeg','-y','-f','concat','-safe','0','-i',lst,'-c','copy',out],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            return jsonify({'error': r.stderr[-600:]}), 500
        merged = base64.b64encode(open(out,'rb').read()).decode()
        return jsonify({'merged': merged,
                        'size_mb': round(os.path.getsize(out)/1024/1024, 2),
                        'chunks_merged': len(chunks_data)})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
