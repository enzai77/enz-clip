from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import subprocess
import os
import uuid
import json

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = "downloads"
CLIPS_FOLDER = "clips"
RANKING_FOLDER = "rankings"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(CLIPS_FOLDER, exist_ok=True)
os.makedirs(RANKING_FOLDER, exist_ok=True)


def get_video_info(url):
    ydl_opts = {'quiet': True, 'skip_download': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return info


def download_low_quality(url, output_path):
    ydl_opts = {
        'format': 'worstvideo[height<=360][ext=mp4]+worstaudio[ext=m4a]/worst[height<=360]/worst',
        'outtmpl': output_path,
        'quiet': False,
        'merge_output_format': 'mp4',
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


def extract_audio(video_path, audio_path):
    subprocess.run([
        'ffmpeg', '-y', '-i', video_path,
        '-vn', '-ar', '16000', '-ac', '1', '-f', 'wav', audio_path
    ], capture_output=True)


def analyze_audio_energy(audio_path, total_duration):
    result = subprocess.run([
        'ffmpeg', '-i', audio_path,
        '-af', 'silencedetect=noise=-30dB:d=0.5',
        '-f', 'null', '-'
    ], capture_output=True, text=True)

    stderr = result.stderr
    silence_starts = []
    for line in stderr.split('\n'):
        if 'silence_start' in line:
            try:
                t = float(line.split('silence_start:')[1].strip())
                silence_starts.append(t)
            except:
                pass

    step = 5.0
    energy_map = {}
    t = 0
    while t < total_duration:
        energy_map[t] = 1.0
        t += step

    for ss in silence_starts:
        bucket = round(ss / step) * step
        for b in energy_map:
            if abs(b - bucket) < step * 2:
                energy_map[b] = max(0.1, energy_map.get(b, 1.0) - 0.5)

    return energy_map


def detect_moments(video_path, total_duration, clip_duration, count, rank_style):
    audio_path = video_path.replace('.mp4', '.wav')
    print("Extracting audio...")
    extract_audio(video_path, audio_path)
    print("Analyzing audio...")
    energy_map = analyze_audio_energy(audio_path, total_duration)
    if os.path.exists(audio_path):
        os.remove(audio_path)

    step = 5.0
    candidates = []
    times = sorted(energy_map.keys())

    for t in times:
        if t + clip_duration > total_duration or t < 5:
            continue
        window_energy = [energy_map[wt] for wt in times if t <= wt < t + clip_duration]
        if not window_energy:
            continue
        avg_energy = sum(window_energy) / len(window_energy)
        peak_energy = max(window_energy)

        if rank_style == 'funny':
            variance = sum((e - avg_energy)**2 for e in window_energy) / len(window_energy)
            score = variance * 2 + peak_energy * 0.5
        elif rank_style == 'emotional':
            score = avg_energy * 0.8 + peak_energy * 0.2
        elif rank_style == 'info':
            variance = sum((e - avg_energy)**2 for e in window_energy) / len(window_energy)
            score = avg_energy * 0.7 - variance * 0.3
        else:
            score = avg_energy * 0.4 + peak_energy * 0.6

        candidates.append({'start': t, 'score': score})

    candidates.sort(key=lambda x: x['score'], reverse=True)

    selected = []
    for c in candidates:
        if not any(abs(c['start'] - s['start']) < clip_duration for s in selected):
            selected.append(c)
        if len(selected) >= count:
            break

    while len(selected) < count:
        segment = total_duration / (count + 1)
        selected.append({'start': segment * (len(selected) + 1), 'score': 0.5})

    if selected:
        max_s = max(x['score'] for x in selected)
        min_s = min(x['score'] for x in selected)
        for m in selected:
            m['viral_score'] = round(65 + (m['score'] - min_s) / (max_s - min_s) * 34, 1) if max_s != min_s else 80.0

    return selected[:count]


def cut_clip(input_path, start, duration, clip_name):
    output_path = os.path.join(CLIPS_FOLDER, f"{clip_name}.mp4")
    subprocess.run([
        'ffmpeg', '-y',
        '-ss', str(int(start)),
        '-i', input_path,
        '-t', str(int(duration)),
        '-c:v', 'libx264', '-c:a', 'aac',
        '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        '-vf', 'scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2,setsar=1,drawtext=text=\'ENZ CLIP\':fontsize=28:fontcolor=white@0.6:x=20:y=H-40:font=Arial:box=1:boxcolor=black@0.3:boxborderw=6',
        output_path
    ], capture_output=True)
    return output_path


def add_ranking_overlay(clip_path, rank_num, title, total_clips, output_path, rank_style):
    """Add ranking number + title text overlay to clip"""

    # Color schemes per style
    colors = {
        'viral': {'num': 'yellow', 'title': 'white', 'bg': '0x000000@0.5'},
        'funny': {'num': 'yellow', 'title': 'white', 'bg': '0x8B0000@0.5'},
        'emotional': {'num': 'cyan', 'title': 'white', 'bg': '0x00008B@0.5'},
        'info': {'num': 'lime', 'title': 'white', 'bg': '0x006400@0.5'},
    }
    c = colors.get(rank_style, colors['viral'])

    # Number colors like TikTok ranking (1=gold, 2=silver, 3=bronze, rest=white)
    num_colors = {1: 'gold', 2: 'silver', 3: '#CD7F32', 4: 'deeppink', 5: 'dodgerblue'}
    num_color = num_colors.get(rank_num, 'white')

    # Safe title — escape special chars
    safe_title = title.replace("'", "").replace('"', '').replace(':', ' -').replace('(', '').replace(')', '')
    # Remove emojis for ffmpeg compatibility
    safe_title = safe_title.encode('ascii', 'ignore').decode('ascii').strip()
    if not safe_title:
        safe_title = f"Moment {rank_num}"

    rank_text = f"#{rank_num}"

    vf = (
        # Dark gradient bar at top
        f"drawbox=x=0:y=0:w=iw:h=160:color={c['bg']}:t=fill,"
        # Rank number - big and bold
        f"drawtext=text='{rank_text}':fontsize=90:fontcolor={num_color}:"
        f"x=20:y=20:font=Arial:box=0,"
        # Title text
        f"drawtext=text='{safe_title}':fontsize=38:fontcolor={c['title']}:"
        f"x=130:y=55:font=Arial:box=1:boxcolor=black@0.3:boxborderw=5"
    )

    subprocess.run([
        'ffmpeg', '-y',
        '-i', clip_path,
        '-vf', vf,
        '-c:v', 'libx264', '-c:a', 'aac',
        '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        output_path
    ], capture_output=True)

    return output_path


def create_ranking_video(ranked_clips, video_id, rank_style, video_title):
    """Join all ranked clips into one final ranking video"""
    print("Creating ranking video...")

    # Write concat file
    concat_path = os.path.join(RANKING_FOLDER, f"{video_id}_concat.txt")
    with open(concat_path, 'w') as f:
        for clip_path in ranked_clips:
            abs_path = os.path.abspath(clip_path).replace('\\', '/')
            f.write(f"file '{abs_path}'\n")

    output_path = os.path.join(RANKING_FOLDER, f"{video_id}_ranking.mp4")
    subprocess.run([
        'ffmpeg', '-y',
        '-f', 'concat', '-safe', '0',
        '-i', concat_path,
        '-c:v', 'libx264', '-c:a', 'aac',
        '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
        output_path
    ], capture_output=True)

    if os.path.exists(concat_path):
        os.remove(concat_path)

    return output_path


@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.json
    url = data.get('url')
    if not url:
        return jsonify({'error': 'No URL'}), 400
    try:
        info = get_video_info(url)
        return jsonify({
            'title': info.get('title'),
            'duration': info.get('duration'),
            'thumbnail': info.get('thumbnail'),
            'channel': info.get('uploader'),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/generate', methods=['POST'])
def generate():
    data = request.json
    url = data.get('url')
    clip_duration = int(data.get('duration', 30))
    count = int(data.get('count', 5))
    rank_style = data.get('rankStyle', 'viral')

    if not url:
        return jsonify({'error': 'No URL'}), 400

    try:
        print("Getting video info...")
        info = get_video_info(url)
        total_duration = info.get('duration', 600)
        title = info.get('title', 'Video')
        video_id = str(uuid.uuid4())[:8]

        video_path = os.path.join(DOWNLOAD_FOLDER, f"{video_id}.mp4")
        print(f"Downloading... ({int(total_duration//60)} min)")
        download_low_quality(url, video_path)

        if not os.path.exists(video_path):
            return jsonify({'error': 'Download failed'}), 500

        print(f"Downloaded! {os.path.getsize(video_path)//(1024*1024)}MB")

        moments = detect_moments(video_path, total_duration, clip_duration, count, rank_style)

        # Style titles
        style_titles = {
            'viral': ['Most Viral Moment', 'Peak Energy Clip', 'Explosive Moment', 'Top Viral Clip', 'High Impact Scene', 'Standout Moment', 'Must Watch Clip', 'Star Moment', 'Best Clip', 'Diamond Moment'],
            'funny': ['Funniest Moment', 'LOL Clip', 'Comedy Gold', 'Hilarious Scene', 'Best Laugh', 'Comedy Peak', 'Silly Moment', 'Crazy Scene', 'Fun Clip', 'Playful Moment'],
            'emotional': ['Most Emotional Scene', 'Touching Moment', 'Heartfelt Clip', 'Moving Scene', 'Inspiring Moment', 'Powerful Scene', 'Beautiful Moment', 'Uplifting Clip', 'Meaningful Scene', 'Soulful Moment'],
            'info': ['Key Insight', 'Must Know Fact', 'Smart Takeaway', 'Important Point', 'Core Message', 'Pro Tip', 'Key Moment', 'Main Point', 'Big Idea', 'How It Works'],
        }
        titles = style_titles.get(rank_style, style_titles['viral'])

        clips = []
        ranked_clip_paths = []

        # Cut clips (worst to best order for ranking reveal)
        for i, moment in enumerate(moments):
            rank_num = i + 1  # 1 = best
            clip_name = f"{video_id}_clip{rank_num}"
            print(f"Cutting clip #{rank_num}...")
            clip_path = cut_clip(video_path, moment['start'], clip_duration, clip_name)

            if os.path.exists(clip_path) and os.path.getsize(clip_path) > 1000:
                # Add ranking overlay
                title_text = titles[i % len(titles)]
                overlay_path = os.path.join(CLIPS_FOLDER, f"{video_id}_ranked{rank_num}.mp4")
                print(f"Adding overlay #{rank_num}: {title_text}")
                add_ranking_overlay(clip_path, rank_num, title_text, count, overlay_path, rank_style)

                if os.path.exists(overlay_path) and os.path.getsize(overlay_path) > 1000:
                    ranked_clip_paths.append(overlay_path)
                    clips.append({
                        'id': f"{video_id}_ranked{rank_num}",
                        'title': f"#{rank_num} {title_text}",
                        'score': round(moment.get('viral_score', 80), 1),
                        'duration': f"0:{clip_duration:02d}",
                        'download_url': f"/download/{video_id}_ranked{rank_num}"
                    })
                    print(f"Clip #{rank_num} ready!")
                else:
                    # fallback to non-overlay
                    ranked_clip_paths.append(clip_path)

        # Create combined ranking video
        ranking_video_path = None
        if ranked_clip_paths:
            print("Joining all clips into ranking video...")
            ranking_video_path = create_ranking_video(ranked_clip_paths, video_id, rank_style, title)

        os.remove(video_path)
        print("All done!")

        response = {
            'success': True,
            'video_title': title,
            'clips': clips,
        }

        if ranking_video_path and os.path.exists(ranking_video_path) and os.path.getsize(ranking_video_path) > 1000:
            response['ranking_video'] = f"/download_ranking/{video_id}_ranking"
            print(f"Ranking video ready! {os.path.getsize(ranking_video_path)//(1024*1024)}MB")

        return jsonify(response)

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/download/<clip_name>', methods=['GET'])
def download(clip_name):
    clip_path = os.path.join(CLIPS_FOLDER, f"{clip_name}.mp4")
    if os.path.exists(clip_path):
        return send_file(clip_path, as_attachment=True, download_name=f"{clip_name}.mp4")
    return jsonify({'error': 'Clip not found'}), 404


@app.route('/download_ranking/<video_id>', methods=['GET'])
def download_ranking(video_id):
    ranking_path = os.path.join(RANKING_FOLDER, f"{video_id}.mp4")
    if os.path.exists(ranking_path):
        return send_file(ranking_path, as_attachment=True, download_name=f"{video_id}.mp4")
    return jsonify({'error': 'Ranking video not found'}), 404


if __name__ == '__main__':
    print("ENZ Clip Backend v5 - Ranking Video Generator!")
    print("Running on http://localhost:5000")
    app.run(debug=True, port=5000)