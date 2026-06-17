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
    # היעד הוא תחילת השעה הנוכחית + TARGET_SECOND
    target = now.replace(minute=0, second=TARGET_SECOND, microsecond=0)
    # אם כבר עברנו את היעד של השעה הזו - היעד הוא השעה הבאה
    if now >= target:
        target = target + timedelta(hours=1)
    return (target - now).total_seconds(), target


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

            # אם הבקשה הגיעה מאוחר (כבר אחרי נקודת ה-pre-roll) - להתחיל מיד
            if wait_to_start < 0:
                wait_to_start = 0

            app.logger.info(f'[{name}] יעד: {target.strftime("%H:%M:%S")} | '
                             f'ממתין {wait_to_start:.1f}ש להתחלת הקלטה')
            if wait_to_start > 0:
                time.sleep(wait_to_start)

            # 2) רגע ההתחלה בפועל של ffmpeg
            actual_start = datetime.now(TZ)
            app.logger.info(f'[{name}] מתחיל ffmpeg ב-{actual_start.strftime("%H:%M:%S")}')

            # 3) הקלטה גולמית (כוללת את ה-pre-roll)
            #    דגלים ל-live: להתחיל מהקצה החי ולא מבאפר ישן
            subprocess.run([
                'ffmpeg', '-y',
                '-live_start_index', '-1',
                '-fflags', 'nobuffer', '-flags', 'low_delay',
                '-i', url,
                '-t', str(RAW_DURATION), '-vn',
                '-acodec', 'libmp3lame', '-ab', '64k',
                '-ar', '22050', '-ac', '1', raw_path
            ], capture_output=True, timeout=RAW_DURATION + 60)

            if not os.path.exists(raw_path) or os.path.getsize(raw_path) < 1000:
                app.logger.error(f'[{name}] הקלטה גולמית נכשלה / ריקה')
                RECORDING_STATUS[name] = 'error'
                return

            # 4) חישוב מאיזו שנייה בקובץ הגולמי לחתוך כדי שהתוצאה תתחיל בדיוק ב-XX:00:05
            #    offset = כמה זמן עבר מתחילת ההקלטה ועד היעד
            offset = (target - actual_start).total_seconds()
            if offset < 0:
                offset = 0   # התחלנו אחרי היעד - חותכים מההתחלה
            app.logger.info(f'[{name}] חיתוך מ-{offset:.2f}ש, אורך {FINAL_DURATION}ש')

            # 5) חיתוך מדויק ל-final_path (re-encode כדי שהחיתוך יהיה מדויק לשנייה)
            subprocess.run([
                'ffmpeg', '-y',
                '-ss', f'{offset:.2f}',
                '-i', raw_path,
                '-t', str(FINAL_DURATION),
                '-acodec', 'libmp3lame', '-ab', '64k',
                '-ar', '22050', '-ac', '1', final_path
            ], capture_output=True, timeout=120)

            # ניקוי הקובץ הגולמי
            try:
                os.remove(raw_path)
            except OSError:
                pass

            if os.path.exists(final_path) and os.path.getsize(final_path) > 1000:
                RECORDINGS[name] = final_path
                RECORDING_STATUS[name] = 'ready'
                app.logger.info(f'[{name}] מוכן: {final_path} '
                                f'({os.path.getsize(final_path)} bytes)')
            else:
                RECORDING_STATUS[name] = 'error'
                app.logger.error(f'[{name}] חיתוך נכשל')

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
    """בדיקת מצב ההקלטה - שימושי לדיבוג מ-GAS."""
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
