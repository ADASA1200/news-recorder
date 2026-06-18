from flask import Flask, request, jsonify, send_file
import subprocess, threading, os, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

app = Flask(__name__)
RECORDINGS = {}          # name -> path של הקובץ החתוך המוכן
RECORDING_STATUS = {}    # name -> 'recording' / 'ready' / 'error'

TZ = ZoneInfo('Asia/Jerusalem')

# ───────── הגדרות תזמון ─────────
# שנייה מדויקת שבה הקובץ הסופי יתחיל (XX:00:05)
TARGET_SECOND   = 5
# כמה שניות לפני היעד להתחיל להקליט בפועל (מרווח ביטחון להתחברות ffmpeg)
PRE_ROLL_SEC    = 8
# אורך הקובץ הסופי בשניות (7 דקות)
FINAL_DURATION  = 7 * 60          # 420
# כמה שניות גולמיות להקליט: pre-roll + אורך סופי + מרווח ביטחון בסוף
RAW_DURATION    = PRE_ROLL_SEC + FINAL_DURATION + 10


@app.route('/ping')
def ping():
    return 'ok'


def seconds_until_target():
    """מחשב כמה שניות נשארו עד XX:00:05 הקרוב (שעה עגולה + TARGET_SECOND)."""
    now = datetime.now(TZ)
    target = now.replace(minute=0, second=TARGET_SECOND, microsecond=0)
    if now >= target:
        target = target + timedelta(hours=1)
    return (target - now).total_seconds(), target


def _stderr_tail(result, n=1500):
    """מחזיר את סוף פלט השגיאה של ffmpeg ללוג."""
    try:
        if result and result.stderr:
            txt = result.stderr.decode('utf-8', 'ignore')
            return txt[-n:]
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

            # 1) חישוב מתי להתחיל להקליט בפועל = pre-roll שניות לפני היעד
            wait_to_target, target = seconds_until_target()
            wait_to_start = wait_to_target - PRE_ROLL_SEC
            if wait_to_start < 0:
                wait_to_start = 0

            app.logger.info(f'[{name}] יעד: {target.strftime("%H:%M:%S")} | '
                             f'ממתין {wait_to_start:.1f}ש להתחלת הקלטה')
            if wait_to_start > 0:
                time.sleep(wait_to_start)

            # 2) רגע ההתחלה בפועל של ffmpeg
            actual_start = datetime.now(TZ)
            app.logger.info(f'[{name}] מתחיל ffmpeg ב-{actual_start.strftime("%H:%M:%S")}')

            # 3) הקלטה גולמית.
            #    דגלי reconnect: גורמים ל-ffmpeg להתחבר מחדש במקום לעצור
            #    על הפרעת רשת / EOF זמני. עובד גם ל-HLS (קול חי) וגם
            #    ל-Icecast (קול ברמה) - בלי דגלים ספציפיים לפורמט אחד.
            result = subprocess.run([
                'ffmpeg', '-y',
                '-reconnect', '1',
                '-reconnect_at_eof', '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', '5',
                '-rw_timeout', '15000000',          # 15ש timeout לקריאה (במיקרו-שניות)
                '-i', url,
                '-t', str(RAW_DURATION), '-vn',
                '-acodec', 'libmp3lame', '-ab', '64k',
                '-ar', '22050', '-ac', '1', raw_path
            ], capture_output=True, timeout=RAW_DURATION + 60)

            raw_size = os.path.getsize(raw_path) if os.path.exists(raw_path) else 0
            app.logger.info(f'[{name}] הקלטה גולמית: {raw_size} bytes, '
                            f'ffmpeg rc={result.returncode}')

            # אם הקובץ ריק/זעיר או ffmpeg נכשל - מדפיסים את שגיאת ffmpeg
            if raw_size < 10000:
                app.logger.error(f'[{name}] הקלטה גולמית נכשלה / קצרה מדי. '
                                 f'ffmpeg stderr:\n{_stderr_tail(result)}')
                RECORDING_STATUS[name] = 'error'
                return

            # 4) חישוב מאיזו שנייה לחתוך כדי שהתוצאה תתחיל בדיוק ב-XX:00:05
            offset = (target - actual_start).total_seconds()
            if offset < 0:
                offset = 0
            app.logger.info(f'[{name}] חיתוך מ-{offset:.2f}ש, אורך {FINAL_DURATION}ש')

            # 5) חיתוך מדויק ל-final_path
            cut = subprocess.run([
                'ffmpeg', '-y',
                '-ss', f'{offset:.2f}',
                '-i', raw_path,
                '-t', str(FINAL_DURATION),
                '-acodec', 'libmp3lame', '-ab', '64k',
                '-ar', '22050', '-ac', '1', final_path
            ], capture_output=True, timeout=120)

            try:
                os.remove(raw_path)
            except OSError:
                pass

            final_size = os.path.getsize(final_path) if os.path.exists(final_path) else 0
            if final_size > 10000:
                RECORDINGS[name] = final_path
                RECORDING_STATUS[name] = 'ready'
                app.logger.info(f'[{name}] מוכן: {final_path} ({final_size} bytes)')
            else:
                app.logger.error(f'[{name}] חיתוך נכשל. ffmpeg stderr:\n{_stderr_tail(cut)}')
                RECORDING_STATUS[name] = 'error'

        except subprocess.TimeoutExpired:
            app.logger.error(f'[{name}] timeout בהקלטה')
            RECORDING_STATUS[name] = 'error'
        except Exception as e:
            app.logger.error(f'[{name}] שגיאה: {e}')
            RECORDING_STATUS[name] = 'error'

    threading.Thread(target=do_record, daemon=True).start()
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
