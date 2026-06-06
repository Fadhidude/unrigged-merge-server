from flask import Flask, request, jsonify
import base64, subprocess, tempfile, os

app = Flask(__name__)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'Unrigged Audio Merge'})

@app.route('/merge', methods=['POST'])
def merge():
    data = request.get_json()
    if not data or 'chunks' not in data:
        return jsonify({'error': 'Missing chunks array'}), 400

    chunks = data['chunks']
    if len(chunks) < 2:
        # If only 1 chunk, return it directly
        return jsonify({'merged': chunks[0], 'chunks_merged': 1})

    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i, b64 in enumerate(chunks):
            p = os.path.join(tmp, f'chunk_{i:02d}.mp3')
            with open(p, 'wb') as f:
                f.write(base64.b64decode(b64))
            paths.append(p)

        # ffmpeg concat list
        lst = os.path.join(tmp, 'list.txt')
        with open(lst, 'w') as f:
            for p in paths:
                f.write(f"file '{p}'\n")

        out = os.path.join(tmp, 'merged.mp3')
        result = subprocess.run(
            ['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
             '-i', lst, '-c', 'copy', out],
            capture_output=True, text=True, timeout=180
        )

        if result.returncode != 0:
            return jsonify({'error': result.stderr[-800:]}), 500

        with open(out, 'rb') as f:
            merged_b64 = base64.b64encode(f.read()).decode()

        size_mb = round(os.path.getsize(out) / 1024 / 1024, 2)
        return jsonify({
            'merged': merged_b64,
            'size_mb': size_mb,
            'chunks_merged': len(chunks)
        })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
