from flask import Flask, request, jsonify, send_file
import subprocess, threading, os, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

app = Flask(__name__)
RECORDINGS = {}          # name -> path של הקובץ המוכן
RECORDING_STATUS = {}    # name -> 'recording' / 'ready' / 'error'

TZ = ZoneInfo('Asia/Jerusalem')

# ───────── הגדרות תזמון ─────────
TARGET_SECOND   = 5            # השנייה שבה הקובץ הסופי יתחיל (XX:00:05)
PRE_ROLL_SEC    = 8            # כמה שניות לפני היעד להתחיל להקליט בפועל
FINAL_DURATION  = 7 * 60       # אורך הקובץ הסופי (7 דקות = 420ש)
RAW_DURATION    = PRE_ROLL_SEC + FINAL_DURATION + 10   # אורך ההקלטה הגולמית


@app.route('/ping')
def ping():
    return 'ok'


def seconds_until_target():
    """כמה שניות עד XX:00:05 הקרוב, ומה התאריך-שעה של היעד."""
    now = datetime.now(TZ)
    target = now.replace(minute=0, second=TARGET_SECOND, microsecond=0)
    if now >= target:
        target = target + timedelta(hours=1)
    return (target - now).total_seconds(), target


def _stderr_tail(result, n=1500):
    try:
        if result and result.stderr:
            return result.stderr.decode('utf-8', 'ignore')[-n:]
    except Exception:
        pass
    return '(אין פלט שגיאה)'


@app.route('/record', methods=['POST'])
def record():
    data = request.json
    name, url = data['name'], data['url']

    raw_path   = f'/tmp/news_{name}_raw.mp3'
    final_path = f'/tmp/news_{name}.mp3'

    def do_record():
        try:
            RECORDING_STATUS[name] = 'recording'

            # 1) ממתינים עד PRE_ROLL שניות לפני היעד
            wait_to_target, target = seconds_until_target()
            wait_to_start = max(0, wait_to_target - PRE_ROLL_SEC)
            app.logger.info(f'[{name}] יעד: {target.strftime("%H:%M:%S")} | '
                            f'ממתין {wait_to_start:.1f}ש')
            if wait_to_start > 0:
                time.sleep(wait_to_start)

            # 2) רגע ההתחלה בפועל
            actual_start = datetime.now(TZ)
            app.logger.info(f'[{name}] מתחיל ffmpeg ב-{actual_start.strftime("%H:%M:%S")}')

            # 3) הקלטה גולמית — פקודה זהה למקור שעבד, ללא דגלים מיוחדים.
            #    עובדת גם ל-HLS (קול חי) וגם ל-Icecast (קול ברמה).
            result = subprocess.run([
                'ffmpeg', '-y', '-i', url,
                '-t', str(RAW_DURATION), '-vn',
                '-acodec', 'libmp3lame', '-ab', '64k',
                '-ar', '22050', '-ac', '1', raw_path
            ], capture_output=True, timeout=RAW_DURATION + 60)

            raw_size = os.path.getsize(raw_path) if os.path.exists(raw_path) else 0
            app.logger.info(f'[{name}] גלם: {raw_size} bytes, ffmpeg rc={result.returncode}')

            if raw_size < 10000:
                app.logger.error(f'[{name}] הקלטה נכשלה. stderr:\n{_stderr_tail(result)}')
                RECORDING_STATUS[name] = 'error'
                return

            # 4) חיתוך מדויק כך שהתוצאה מתחילה ב-XX:00:05
            offset = max(0, (target - actual_start).total_seconds())
            app.logger.info(f'[{name}] חיתוך מ-{offset:.2f}ש, אורך {FINAL_DURATION}ש')

            cut = subprocess.run([
                'ffmpeg', '-y',
                '-ss', f'{offset:.2f}',
                '-i', raw_path,
                '-t', str(FINAL_DURATION),
                '-acodec', 'libmp3lame', '-ab', '64k',
                '-ar', '22050', '-ac', '1', final_path
            ], capture_output=True, timeout=120)

            final_size = os.path.getsize(final_path) if os.path.exists(final_path) else 0

            if final_size > 10000:
                # חיתוך הצליח - משתמשים בקובץ החתוך, מוחקים את הגלם
                RECORDINGS[name] = final_path
                RECORDING_STATUS[name] = 'ready'
                app.logger.info(f'[{name}] מוכן (חתוך): {final_size} bytes')
                try:
                    os.remove(raw_path)
                except OSError:
                    pass
            else:
                # חיתוך נכשל - גיבוי: משתמשים בקובץ הגולמי כדי לא לאבד הקלטה
                app.logger.error(f'[{name}] חיתוך נכשל, משתמש בגלם. stderr:\n{_stderr_tail(cut)}')
                RECORDINGS[name] = raw_path
                RECORDING_STATUS[name] = 'ready'

        except subprocess.TimeoutExpired:
            app.logger.error(f'[{name}] timeout בהקלטה')
            RECORDING_STATUS[name] = 'error'
        except Exception as e:
            app.logger.error(f'[{name}] שגיאה: {e}')
            RECORDING_STATUS[name] = 'error'

    threading.Thread(target=do_record).start()
    return jsonify({'status': 'recording_started'})


@app.route('/status')
def status():
    name = request.args.get('name')
    return jsonify({'status': RECORDING_STATUS.get(name, 'unknown')})


@app.route('/download')
def download():
    name = request.args.get('name')
    path = RECORDINGS.get(name)
    if path and os.path.exists(path):
        return send_file(path, mimetype='audio/mpeg')
    return jsonify({'error': 'not_ready',
                    'status': RECORDING_STATUS.get(name, 'unknown')}), 404


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
