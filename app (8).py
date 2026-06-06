import subprocess, tempfile, os, json, io, requests, math, threading, uuid
from flask import Flask, request, jsonify

app = Flask(__name__)

SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')
AUDIO_BUCKET = 'unrigged-audio'
VIDEO_BUCKET = 'unrigged-video'

jobs = {}  # In-memory job store: { job_id: { status, url, error, progress } }

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
    return jsonify({'status': 'ok', 'service': 'Unrigged Merge v10 (async)'})

# ── JOB STATUS ───────────────────────────────────────────────────────────────
@app.route('/job-status/<job_id>')
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'status': 'not_found'}), 404
    return jsonify(job)

# ── AUDIO MERGE ──────────────────────────────────────────────────────────────
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

# ── VIDEO ASSEMBLY (asynkron) ─────────────────────────────────────────────────
def _assemble_worker(job_id, data):
    clip_urls = data.get('clip_urls', [])
    audio_url = data.get('audio_url', '')
    variant   = data.get('title', 'Unrigged')

    try:
        jobs[job_id]['progress'] = 'Laddar ner audio...'
        with tempfile.TemporaryDirectory() as tmp:

            # 1. Ladda ner audio
            audio_path = os.path.join(tmp, 'audio.mp3')
            r = requests.get(audio_url, timeout=60)
            open(audio_path,'wb').write(r.content)

            probe = subprocess.run(
                ['ffprobe','-v','quiet','-print_format','json','-show_format', audio_path],
                capture_output=True, text=True)
            audio_dur = float(json.loads(probe.stdout)['format']['duration'])

            # 2. Ladda ner klipp
            jobs[job_id]['progress'] = f'Laddar ner {len(clip_urls)} klipp...'
            clip_paths = []
            for i, url in enumerate(clip_urls):
                p = os.path.join(tmp, f'clip_{i:02d}.mp4')
                r = requests.get(url, timeout=60)
                if r.status_code == 200:
                    open(p,'wb').write(r.content)
                    clip_paths.append(p)

            if not clip_paths:
                jobs[job_id] = {'status': 'error', 'error': 'Inga klipp laddades ner'}
                return

            # 3. Normalisera klipp (ColdFusion color grade)
            jobs[job_id]['progress'] = 'Normaliserar klipp...'
            norm_paths = []
            for i, cp in enumerate(clip_paths):
                np_ = os.path.join(tmp, f'norm_{i:02d}.mp4')
                subprocess.run([
                    'ffmpeg','-y','-i',cp,
                    '-vf',(
                        'scale=1920:1080:force_original_aspect_ratio=decrease,'
                        'pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,'
                        'curves=preset=cross_process,'
                        'eq=brightness=-0.05:contrast=1.15:saturation=0.75'
                    ),
                    '-c:v','libx264','-preset','fast','-crf','20','-an', np_
                ], capture_output=True, timeout=120)
                if os.path.exists(np_) and os.path.getsize(np_) > 0:
                    norm_paths.append(np_)

            if not norm_paths:
                jobs[job_id] = {'status': 'error', 'error': 'Normalisering misslyckades'}
                return

            # 4. Loopa klipp för att täcka audio-längd
            jobs[job_id]['progress'] = 'Monterar video...'
            total_clip = sum(
                float(json.loads(subprocess.run(
                    ['ffprobe','-v','quiet','-print_format','json','-show_format',p],
                    capture_output=True,text=True).stdout).get('format',{}).get('duration',0))
                for p in norm_paths)

            if total_clip <= 0: total_clip = 10
            loops = math.ceil(audio_dur / total_clip) + 1
            looped = norm_paths * loops

            lst = os.path.join(tmp, 'vlist.txt')
            open(lst,'w').write('\n'.join(f"file '{p}'" for p in looped))

            concat = os.path.join(tmp, 'concat.mp4')
            subprocess.run(['ffmpeg','-y','-f','concat','-safe','0','-i',lst,
                            '-c','copy', concat], capture_output=True, timeout=300)

            # 5. Mixa video + audio
            output = os.path.join(tmp, 'final.mp4')
            subprocess.run([
                'ffmpeg','-y','-i',concat,'-i',audio_path,
                '-map','0:v','-map','1:a',
                '-c:v','copy','-c:a','aac','-b:a','192k',
                '-shortest', output
            ], capture_output=True, timeout=600)

            if not os.path.exists(output) or os.path.getsize(output) == 0:
                jobs[job_id] = {'status': 'error', 'error': 'Video-montering misslyckades'}
                return

            # 6. Ladda upp till Supabase
            jobs[job_id]['progress'] = 'Laddar upp till Supabase...'
            size_mb  = round(os.path.getsize(output)/1024/1024, 2)
            filename = f'video_{variant[:15].replace(" ","_")}.mp4'
            supabase_upload(output, VIDEO_BUCKET, filename, 'video/mp4')

            url = f"{SUPABASE_URL}/storage/v1/object/public/{VIDEO_BUCKET}/{filename}"
            jobs[job_id] = {
                'status': 'done',
                'url': url,
                'size_mb': size_mb,
                'clips_used': len(norm_paths),
                'duration_sec': round(audio_dur),
                'variant': variant
            }

    except Exception as e:
        jobs[job_id] = {'status': 'error', 'error': str(e)[:300]}

@app.route('/assemble-video', methods=['POST'])
def assemble_video():
    data   = request.get_json()
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {'status': 'processing', 'progress': 'Startar...'}
    t = threading.Thread(target=_assemble_worker, args=(job_id, data), daemon=True)
    t.start()
    return jsonify({'job_id': job_id, 'status': 'processing'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
