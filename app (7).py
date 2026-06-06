import subprocess, tempfile, os, json, io, requests, math
from flask import Flask, request, jsonify

app = Flask(__name__)

SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')
AUDIO_BUCKET = 'unrigged-audio'
VIDEO_BUCKET = 'unrigged-video'

def drive_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    sa = json.loads(os.environ.get('GOOGLE_SA_KEY', '{}'))
    creds = service_account.Credentials.from_service_account_info(
        sa, scopes=['https://www.googleapis.com/auth/drive.readonly'])
    return build('drive', 'v3', credentials=creds, cache_discovery=False)

def supabase_upload(local_path, bucket, filename, mime):
    with open(local_path, 'rb') as f:
        r = requests.put(
            f"{SUPABASE_URL}/storage/v1/object/{bucket}/{filename}",
            headers={'Authorization': f'Bearer {SUPABASE_KEY}',
                     'Content-Type': mime, 'x-upsert': 'true'},
            data=f)
    return r.status_code in (200, 201)

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'Unrigged Merge v9 (video)'})

# ── AUDIO MERGE (befintlig) ──────────────────────────────────────────────────
@app.route('/merge-from-drive', methods=['POST'])
def merge_from_drive():
    from googleapiclient.http import MediaIoBaseDownload
    data      = request.get_json()
    file_ids  = data.get('file_ids', [])
    bg_volume = data.get('bg_volume', 0.08)
    if not file_ids:
        return jsonify({'error': 'No file_ids provided'}), 400

    svc = drive_service()
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for i, fid in enumerate(file_ids):
            p = os.path.join(tmp, f'chunk_{i:02d}.mp3')
            req = svc.files().get_media(fileId=fid)
            buf = io.BytesIO()
            dl  = MediaIoBaseDownload(buf, req)
            done = False
            while not done: _, done = dl.next_chunk()
            open(p, 'wb').write(buf.getvalue())
            paths.append(p)

        if len(paths) == 1:
            voice_path = paths[0]
        else:
            lst = os.path.join(tmp, 'list.txt')
            open(lst,'w').write('\n'.join(f"file '{p}'" for p in paths))
            voice_path = os.path.join(tmp, 'voice_merged.mp3')
            subprocess.run(['ffmpeg','-y','-f','concat','-safe','0',
                            '-i',lst,'-c','copy',voice_path],
                           capture_output=True, timeout=180)

        # Bakgrundsmusik
        bg_path = os.path.join(tmp, 'bg.mp3')
        bg_url  = f"{SUPABASE_URL}/storage/v1/object/public/{AUDIO_BUCKET}/background.mp3"
        has_bg  = False
        try:
            r = requests.get(bg_url, timeout=15)
            if r.status_code == 200:
                open(bg_path,'wb').write(r.content); has_bg = True
        except: pass

        merged = os.path.join(tmp, 'unrigged_final.mp3')
        if has_bg:
            subprocess.run(['ffmpeg','-y','-i',voice_path,
                            '-stream_loop','-1','-i',bg_path,
                            '-filter_complex',
                            f'[0:a]volume=1.0[v];[1:a]volume={bg_volume}[b];[v][b]amix=inputs=2:duration=first',
                            '-c:a','libmp3lame','-q:a','2', merged],
                           capture_output=True, timeout=180)
        else:
            os.rename(voice_path, merged)

        size_mb = round(os.path.getsize(merged)/1024/1024, 2)
        supabase_upload(merged, AUDIO_BUCKET, 'unrigged_final.mp3', 'audio/mpeg')
        return jsonify({
            'ok': True,
            'url': f"{SUPABASE_URL}/storage/v1/object/public/{AUDIO_BUCKET}/unrigged_final.mp3",
            'size_mb': size_mb, 'chunks_merged': len(paths), 'background_mixed': has_bg
        })

# ── VIDEO ASSEMBLY (ny) ──────────────────────────────────────────────────────
@app.route('/assemble-video', methods=['POST'])
def assemble_video():
    data       = request.get_json()
    clip_urls  = data.get('clip_urls', [])   # Pexels-klipp i Supabase
    audio_url  = data.get('audio_url', '')   # Merged MP3 URL
    title      = data.get('title', 'Unrigged')

    if not clip_urls or not audio_url:
        return jsonify({'error': 'clip_urls and audio_url required'}), 400

    with tempfile.TemporaryDirectory() as tmp:
        # 1. Ladda ner audio
        audio_path = os.path.join(tmp, 'audio.mp3')
        r = requests.get(audio_url, timeout=60)
        open(audio_path,'wb').write(r.content)

        # Hämta audio-längd
        probe = subprocess.run(
            ['ffprobe','-v','quiet','-print_format','json','-show_format', audio_path],
            capture_output=True, text=True)
        audio_dur = float(json.loads(probe.stdout)['format']['duration'])

        # 2. Ladda ner klipp
        clip_paths = []
        for i, url in enumerate(clip_urls):
            p = os.path.join(tmp, f'clip_{i:02d}.mp4')
            r = requests.get(url, timeout=60)
            if r.status_code == 200:
                open(p,'wb').write(r.content)
                clip_paths.append(p)

        if not clip_paths:
            return jsonify({'error': 'No clips downloaded'}), 500

        # 3. Normalisera klipp till 1920x1080 + ColdFusion color grade
        norm_paths = []
        for i, cp in enumerate(clip_paths):
            np_ = os.path.join(tmp, f'norm_{i:02d}.mp4')
            subprocess.run([
                'ffmpeg','-y','-i',cp,
                '-vf', (
                    'scale=1920:1080:force_original_aspect_ratio=decrease,'
                    'pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,'
                    'curves=preset=cross_process,'
                    'eq=brightness=-0.05:contrast=1.15:saturation=0.75'
                ),
                '-c:v','libx264','-preset','fast','-crf','20',
                '-an', np_
            ], capture_output=True, timeout=120)
            if os.path.exists(np_): norm_paths.append(np_)

        # 4. Loopa klipp för att täcka audio-längden
        total_clip = sum(
            float(json.loads(subprocess.run(
                ['ffprobe','-v','quiet','-print_format','json','-show_format',p],
                capture_output=True,text=True).stdout)['format']['duration'])
            for p in norm_paths)

        loops = math.ceil(audio_dur / total_clip) + 1
        looped = norm_paths * loops

        lst = os.path.join(tmp, 'vlist.txt')
        open(lst,'w').write('\n'.join(f"file '{p}'" for p in looped))

        # 5. Concat video
        concat_path = os.path.join(tmp, 'concat.mp4')
        subprocess.run(['ffmpeg','-y','-f','concat','-safe','0','-i',lst,
                        '-c','copy', concat_path],
                       capture_output=True, timeout=300)

        # 6. Mixa video + audio, trim till audio-längd
        output = os.path.join(tmp, 'unrigged_final.mp4')
        subprocess.run([
            'ffmpeg','-y',
            '-i', concat_path,
            '-i', audio_path,
            '-map','0:v','-map','1:a',
            '-c:v','copy','-c:a','aac','-b:a','192k',
            '-shortest',
            output
        ], capture_output=True, timeout=600)

        if not os.path.exists(output):
            return jsonify({'error': 'Video assembly failed'}), 500

        size_mb  = round(os.path.getsize(output)/1024/1024, 2)
        filename = f'unrigged_final_{title[:20]}.mp4'
        supabase_upload(output, VIDEO_BUCKET, filename, 'video/mp4')

        return jsonify({
            'ok': True,
            'url': f"{SUPABASE_URL}/storage/v1/object/public/{VIDEO_BUCKET}/{filename}",
            'size_mb': size_mb,
            'clips_used': len(norm_paths),
            'duration_sec': round(audio_dur)
        })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
