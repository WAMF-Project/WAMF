from flask import Flask, render_template, request, redirect, url_for, send_file, abort, send_from_directory, jsonify
import os
import sqlite3
import base64
from datetime import datetime, date
import yaml
import requests
from io import BytesIO
from queries import recent_detections, get_daily_summary, get_common_name, get_records_for_date_hour
from queries import get_records_for_scientific_name_and_date, get_earliest_detection_date

app = Flask(__name__)
config = None
DBPATH = './data/speciesid.db'
NAMEDBPATH = './birdnames.db'


def format_datetime(value, format='%B %d, %Y %H:%M:%S'):
    dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S.%f')
    return dt.strftime(format)


app.jinja_env.filters['datetime'] = format_datetime


@app.route('/')
def index():
    today = datetime.now()
    date_str = today.strftime('%Y-%m-%d')
    earliest_date = get_earliest_detection_date()
    recent_records = recent_detections(5)
    daily_summary = get_daily_summary(today)
    return render_template('index.html', recent_detections=recent_records, daily_summary=daily_summary,
                           current_hour=today.hour, date=date_str, earliest_date=earliest_date)


@app.route('/frigate/<frigate_event>/thumbnail.jpg')
def frigate_thumbnail(frigate_event):
    frigate_url = config['frigate']['frigate_url']
    try:
        response = requests.get(
            f'{frigate_url}/api/events/{frigate_event}/thumbnail.jpg',
            stream=True, timeout=5)
        if response.status_code == 200:
            return send_file(response.raw, mimetype=response.headers['Content-Type'])
        else:
            return send_from_directory('static/images', '1x1.png', mimetype='image/png')
    except Exception as e:
        print(f"Error fetching thumbnail from frigate: {e}", flush=True)
        return send_from_directory('static/images', '1x1.png', mimetype='image/png')


@app.route('/frigate/<frigate_event>/snapshot.jpg')
def frigate_snapshot(frigate_event):
    frigate_url = config['frigate']['frigate_url']
    try:
        response = requests.get(
            f'{frigate_url}/api/events/{frigate_event}/snapshot.jpg',
            stream=True, timeout=5)
        if response.status_code == 200:
            return send_file(response.raw, mimetype=response.headers['Content-Type'])
        else:
            return send_from_directory('static/images', '1x1.png', mimetype='image/png')
    except Exception as e:
        print(f"Error fetching snapshot from frigate: {e}", flush=True)
        return send_from_directory('static/images', '1x1.png', mimetype='image/png')


@app.route('/frigate/<frigate_event>/clip.mp4')
def frigate_clip(frigate_event):
    frigate_url = config['frigate']['frigate_url']
    try:
        response = requests.get(
            f'{frigate_url}/api/events/{frigate_event}/clip.mp4',
            stream=True, timeout=30)
        if response.status_code == 200:
            return send_file(response.raw, mimetype=response.headers['Content-Type'])
        else:
            return send_from_directory('static/images', '1x1.png', mimetype='image/png')
    except Exception as e:
        print(f"Error fetching clip from frigate: {e}", flush=True)
        return send_from_directory('static/images', '1x1.png', mimetype='image/png')


@app.route('/detections/by_hour/<date>/<int:hour>')
def show_detections_by_hour(date, hour):
    records = get_records_for_date_hour(date, hour)
    return render_template('detections_by_hour.html', date=date, hour=hour, records=records)


@app.route('/detections/by_scientific_name/<scientific_name>/<date>', defaults={'end_date': None})
@app.route('/detections/by_scientific_name/<scientific_name>/<date>/<end_date>')
def show_detections_by_scientific_name(scientific_name, date, end_date):
    if end_date is not None:
        return jsonify({"error": "Date range queries are not yet implemented."}), 501
    records = get_records_for_scientific_name_and_date(scientific_name, date)
    return render_template('detections_by_scientific_name.html', scientific_name=scientific_name, date=date,
                           end_date=end_date, common_name=get_common_name(scientific_name), records=records)


@app.route('/api/detections/recent')
def api_recent_detections():
    limit = request.args.get('limit', 5, type=int)
    records = recent_detections(min(limit, 20))  # cap at 20
    return jsonify(records)


@app.route('/daily_summary')
@app.route('/daily_summary/')
def show_daily_summary_today():
    today = datetime.now().strftime('%Y-%m-%d')
    target = url_for('show_daily_summary', date=today)
    query = request.query_string.decode('utf-8')
    if query:
        target = f'{target}?{query}'
    return redirect(target)


@app.route('/daily_summary/<date>')
def show_daily_summary(date):
    date_datetime = datetime.strptime(date, "%Y-%m-%d")
    daily_summary = get_daily_summary(date_datetime)
    today = datetime.now().strftime('%Y-%m-%d')
    earliest_date = get_earliest_detection_date()
    return render_template('daily_summary.html', daily_summary=daily_summary, date=date, today=today,
                           earliest_date=earliest_date)


@app.route('/detections/<frigate_event>', methods=['DELETE'])
def delete_detection(frigate_event):
    if not frigate_event:
        return jsonify({"success": False, "message": "Missing detection identifier."}), 400

    conn = None
    try:
        conn = sqlite3.connect(DBPATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM detections WHERE frigate_event = ?", (frigate_event,))
        deleted_rows = cursor.rowcount
        conn.commit()
    except sqlite3.Error as e:
        print(f"Error deleting detection '{frigate_event}': {e}", flush=True)
        return jsonify({"success": False, "message": "Unable to delete detection."}), 500
    finally:
        if conn:
            conn.close()

    if deleted_rows == 0:
        return jsonify({"success": False, "message": "Detection not found."}), 404

    return jsonify({
        "success": True,
        "message": "Detection deleted.",
        "frigate_event": frigate_event
    }), 200


def load_config():
    global config
    file_path = os.environ.get('WHOSATMYFEEDER_CONFIG', './config/config.yml')
    with open(file_path, 'r') as config_file:
        config = yaml.safe_load(config_file)


load_config()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7766, debug=True)