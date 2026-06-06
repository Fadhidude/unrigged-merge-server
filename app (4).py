import base64, subprocess, tempfile, os, json, io
from flask import Flask, request, jsonify

app = Flask(__name__)

def drive_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    sa = json.loads(os.environ.get('GOOGLE_SA_KEY', '{}'))
    creds = service_account.Credentials.from_service_account_info(
        sa, scopes=['https://www.googleapis.com/auth/drive.readonly']
    )
    return build('drive', 'v3', credentials=creds, cache_discovery=False)

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'Unrigged Merge v5'})

@app.route('/merge-from-drive', methods=['POST'])
def merge_from_drive():
    from googleapiclient.http import MediaIoBaseDownload
    data = request.get_json()
    file_ids = data.get('file_ids', [])
    if not file_ids:
        return jsonify({'error': 'No file_ids provided'}), 400

    svc = drive_service()

    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i, fid in enumerate(file_ids):
            p = os.path.join(tmp, f'chunk_{i:02d}.mp3')
            req = svc.files().get_media(fileId=fid)
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, req)
            done = False
            while not done:
                _, done = dl.next_chunk()
            open(p, 'wb').write(buf.getvalue())
            paths.append(p)

        if len(paths) == 1:
            merged = base64.b64encode(open(paths[0],'rb').read()).decode()
            return jsonify({'merged': merged, 'size_mb': round(os.path.getsize(paths[0])/1024/1024,2), 'chunks_merged': 1})

        lst = os.path.join(tmp, 'list.txt')
        open(lst,'w').write('\n'.join(f"file '{p}'" for p in paths))
        out = os.path.join(tmp, 'merged.mp3')
        r = subprocess.run(
            ['ffmpeg','-y','-f','concat','-safe','0','-i',lst,'-c','copy',out],
            capture_output=True, text=True, timeout=180
        )
        if r.returncode != 0:
            return jsonify({'error': r.stderr[-500:]}), 500

        merged = base64.b64encode(open(out,'rb').read()).decode()
        return jsonify({
            'merged': merged,
            'size_mb': round(os.path.getsize(out)/1024/1024, 2),
            'chunks_merged': len(paths)
        })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
