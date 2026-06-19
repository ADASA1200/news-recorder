from flask import Flask, request, jsonify, send_file
import subprocess, threading, os, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

app = Flask(__name__)
RECORDINGS = {}          # name -> path של הקובץ המוכן
RECORDING_STATUS = {}    # name -> 'recording' / 'ready' / 'error'

TZ = ZoneInfo('Asia/Jerusalem')

# ───────── הגדרות ─────────
TARGET_SECOND   = 5            # השנייה שבה הקובץ הסופי יתחיל (XX:00:05)
PRE_ROLL_SEC    = 10           # שניות לפני היעד להתחיל להקליט (סופג השהיית התחברות/נפילות)
FINAL_DURATION  = 7 * 60       # אורך הקובץ הסופי (7 דקות)
RAW_DURATION    = PRE_ROLL_SEC + FINAL_DURATION + 15   # אורך ההקלטה הגולמית

# סף מינימלי לקובץ תקין (בייטים). 64kbps ≈ 8KB לשנייה, אז 7 דק' ≈ 3.3MB.
MIN_VALID_BYTES = 200000       # ~25 שניות - אם פחות מזה, נחשב כישלון


@app.route('/ping')
def ping():
    return 'ok'


def seconds_until_target():
    """כמה שניות עד XX:00:05 הקרוב, ומהו התאריך-שעה של היעד."""
    now = datetime.now(TZ)
    target = now.replace(minute=0, second=TARGET_SECOND, microsecond=0)
    if now >= target:
        target = target + timedelta(hours=1)
    return (target - now).total_seconds(), target


def _stderr_tail(result, n=1800):
    try:
        if result and result.stderr:
            return result.stderr.decode('utf-8', 'ignore')[-n:]
    except Exception:
        pass
    return '(אין פלט שגיאה)'


def run_ffmpeg_record(url, out_path, duration):
    """
    הקלטה גולמית מסטרים חי.
    דגלי reconnect: ffmpeg מתחבר מחדש אוטומטית כשהסטרים נופל,
    במקום לעצור. סט נקי שעובד גם ל-HLS (קול חי) וגם ל-Icecast (קול ברמה).
    """
    return subprocess.run([
        'ffmpeg', '-y',
        '-reconnect', '1',
        '-reconnect_streamed', '1',
        '-reconnect_delay_max', '4',
        '-i', url,
        '-t', str(duration), '-vn',
        '-acodec', 'libmp3lame', '-ab', '64k',
        '-ar', '22050', '-ac', '1', out_path
    ], capture_output=True, timeout=duration + 90)


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
            app.logger.info(f'[{name}] יעד: {target.strftime("%H:%M:%S")} | ממתין {wait_to_start:.1f}ש')
            if wait_to_start > 0:
                time.sleep(wait_to_start)

            # 2) הקלטה גולמית (עד 2 ניסיונות אם הראשון יוצא ריק/קצר מדי)
            result = None
            actual_start = None
            for attempt in (1, 2):
                actual_start = datetime.now(TZ)
                app.logger.info(f'[{name}] ניסיון {attempt}: מתחיל ffmpeg ב-{actual_start.strftime("%H:%M:%S")}')
                result = run_ffmpeg_record(url, raw_path, RAW_DURATION)
                raw_size = os.path.getsize(raw_path) if os.path.exists(raw_path) else 0
                app.logger.info(f'[{name}] ניסיון {attempt}: גלם {raw_size} bytes, rc={result.returncode}')
                if raw_size >= MIN_VALID_BYTES:
                    break
                app.logger.error(f'[{name}] ניסיון {attempt} קצר/ריק. stderr:\n{_stderr_tail(result)}')
                # אם נכשל ועוד יש זמן - מנסים שוב מיד (ההקלטה תהיה קצרה יותר אבל עדיף מכלום)

            raw_size = os.path.getsize(raw_path) if os.path.exists(raw_path) else 0
            if raw_size < MIN_VALID_BYTES:
                app.logger.error(f'[{name}] כל הניסיונות נכשלו')
                RECORDING_STATUS[name] = 'error'
                return

            # 3) חיתוך מדויק כך שהתוצאה מתחילה ב-XX:00:05
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
            if final_size >= MIN_VALID_BYTES:
                RECORDINGS[name] = final_path
                RECORDING_STATUS[name] = 'ready'
                app.logger.info(f'[{name}] ✓ מוכן (חתוך): {final_size} bytes')
                try:
                    os.remove(raw_path)
                except OSError:
                    pass
            else:
                # גיבוי: החיתוך נכשל - משתמשים בגלם כדי לא לאבד הקלטה
                app.logger.error(f'[{name}] חיתוך נכשל, משתמש בגלם. stderr:\n{_stderr_tail(cut)}')
                RECORDINGS[name] = raw_path
                RECORDING_STATUS[name] = 'ready'

        except subprocess.TimeoutExpired:
            app.logger.error(f'[{name}] timeout')
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
