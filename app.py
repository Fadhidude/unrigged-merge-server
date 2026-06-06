import subprocess, tempfile, os, json, io, requests
from flask import Flask, request, jsonify

app = Flask(__name__)

SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')
BUCKET      = 'unrigged-audio'

def drive_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    sa = json.loads(os.environ.get('GOOGLE_SA_KEY', '{}'))
    creds = service_account.Credentials.from_service_account_info(
        sa, scopes=['https://www.googleapis.com/auth/drive.readonly'])
    return build('drive', 'v3', credentials=creds, cache_discovery=False)

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'Unrigged Merge v7 (supabase)'})

@app.route('/merge-from-drive', methods=['POST'])
def merge_from_drive():
    from googleapiclient.http import MediaIoBaseDownload
    data = request.get_json()
    file_ids = data.get('file_ids', [])
    if not file_ids:
        return jsonify({'error': 'No file_ids provided'}), 400

    svc = drive_service()

    with tempfile.TemporaryDirectory() as tmp:
        # 1. Download chunks from Drive
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

        # 2. Merge with ffmpeg
        if len(paths) == 1:
            merged_path = paths[0]
        else:
            lst = os.path.join(tmp, 'list.txt')
            open(lst, 'w').write('\n'.join(f"file '{p}'" for p in paths))
            merged_path = os.path.join(tmp, 'unrigged_final.mp3')
            r = subprocess.run(
                ['ffmpeg','-y','-f','concat','-safe','0','-i',lst,'-c','copy',merged_path],
                capture_output=True, text=True, timeout=180
            )
            if r.returncode != 0:
                return jsonify({'error': r.stderr[-500:]}), 500

        size_mb = round(os.path.getsize(merged_path) / 1024 / 1024, 2)

        # 3. Upload to Supabase Storage
        with open(merged_path, 'rb') as f:
            up = requests.put(
                f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/unrigged_final.mp3",
                headers={
                    'Authorization': f'Bearer {SUPABASE_KEY}',
                    'Content-Type': 'audio/mpeg',
                    'x-upsert': 'true'
                },
                data=f
            )
        if up.status_code not in (200, 201):
            return jsonify({'error': f'Supabase upload failed: {up.text}'}), 500

        public_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/unrigged_final.mp3"
        return jsonify({
            'ok': True,
            'url': public_url,
            'size_mb': size_mb,
            'chunks_merged': len(paths)
        })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
