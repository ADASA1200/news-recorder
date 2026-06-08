from flask import Flask, request, jsonify, send_file
import subprocess, threading, os

app = Flask(__name__)
RECORDINGS = {}

@app.route('/ping')
def ping():
    return 'ok'

@app.route('/record', methods=['POST'])
def record():
    data = request.json
    name, url = data['name'], data['url']
    path = f'/tmp/news_{name}.mp3'

    def do_record():
        subprocess.run([
            'ffmpeg', '-y', '-i', url,
            '-t', '301', '-vn',
            '-acodec', 'libmp3lame', '-ab', '64k',
            '-ar', '22050', '-ac', '1', path
        ], capture_output=True, timeout=360)
        RECORDINGS[name] = path

    threading.Thread(target=do_record).start()
    return jsonify({'status': 'recording_started'})

@app.route('/download')
def download():
    name = request.args.get('name')
    path = RECORDINGS.get(name)
    if path and os.path.exists(path):
        return send_file(path, mimetype='audio/mpeg')
    return jsonify({'error': 'not_ready'}), 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)