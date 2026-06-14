"""Public/user-facing routes that remain available without admin auth."""

from datetime import datetime

from flask import Blueprint, jsonify, redirect, render_template, request, url_for


public_bp = Blueprint('public', __name__)


@public_bp.route('/')
def index():
    import webui

    today = datetime.now()
    date_str = today.strftime('%Y-%m-%d')
    earliest_date = webui.get_earliest_detection_date()
    recent_records = webui.recent_detections(4)
    daily_summary = webui.get_daily_summary(today)
    activity_by_hour = webui.get_activity_by_hour(date_str)
    top_species = webui.get_top_species(date_str)
    latest_visitor = webui.get_latest_visitor()

    return render_template(
        'index.html',
        recent_detections=recent_records,
        daily_summary=daily_summary,
        activity_by_hour=activity_by_hour,
        top_species=top_species,
        latest_visitor=latest_visitor,
        current_hour=today.hour,
        date=date_str,
        earliest_date=earliest_date
    )


@public_bp.route('/recent')
def recent_feed():
    import webui

    today = datetime.now()
    date_str = today.strftime('%Y-%m-%d')

    return render_template(
        'recent_feed.html',
        recent_detections=webui.recent_detections(50),
        current_hour=today.hour,
        date=date_str,
        earliest_date=webui.get_earliest_detection_date()
    )


@public_bp.route('/detections/by_hour/<date>/<int:hour>')
def show_detections_by_hour(date, hour):
    import webui

    records = webui.get_records_for_date_hour(date, hour)
    return render_template(
        'detections_by_hour.html',
        date=date,
        hour=hour,
        records=records
    )


@public_bp.route(
    '/detections/by_scientific_name/<scientific_name>/<date>',
    defaults={'end_date': None}
)
@public_bp.route('/detections/by_scientific_name/<scientific_name>/<date>/<end_date>')
def show_detections_by_scientific_name(scientific_name, date, end_date):
    import webui

    page = request.args.get(
        'page',
        1,
        type=int
    )
    webui.logger.debug("scientific_name = [%s]", scientific_name)
    webui.logger.debug("date = [%s]", date)

    if end_date is not None:
        return jsonify({"error": "Date range queries are not yet implemented."}), 501

    per_page = 25
    total_records = (
        webui.get_detection_count_for_scientific_name_and_date(
            scientific_name,
            date
        )
    )

    total_pages = (
        total_records + per_page - 1
    ) // per_page
    records = webui.get_records_for_scientific_name_and_date(
        scientific_name,
        date,
        page,
        per_page
    )
    species_stats = webui.get_species_stats_for_date(scientific_name, date)
    species_info = webui.get_species_info(scientific_name)

    # Metadata can require a network call, so queue it instead of delaying page render.
    if webui.species_needs_metadata(species_info):
        webui.queue_metadata_refresh(scientific_name)

    species_activity = webui.get_species_activity_by_hour(scientific_name)

    return render_template(
        'detections_by_scientific_name.html',
        scientific_name=scientific_name,
        date=date,
        end_date=end_date,
        common_name=webui.get_common_name(scientific_name),
        records=records,
        species_stats=species_stats,
        species_activity=species_activity,
        species_info=species_info,
        page=page,
        total_pages=total_pages,
        total_records=total_records
    )


@public_bp.route('/daily_summary')
@public_bp.route('/daily_summary/')
def show_daily_summary_today():
    today = datetime.now().strftime('%Y-%m-%d')
    target = url_for('public.show_daily_summary', date=today)
    query = request.query_string.decode('utf-8')

    if query:
        target = f'{target}?{query}'

    return redirect(target)


@public_bp.route('/daily_summary/<date>')
def show_daily_summary(date):
    import webui

    date_datetime = datetime.strptime(date, "%Y-%m-%d")
    daily_summary = webui.get_daily_summary(date_datetime)
    today = datetime.now().strftime('%Y-%m-%d')
    earliest_date = webui.get_earliest_detection_date()

    return render_template(
        'daily_summary.html',
        daily_summary=daily_summary,
        date=date,
        today=today,
        earliest_date=earliest_date
    )


@public_bp.route('/activity')
def activity():
    import webui

    today = datetime.now()
    date_str = today.strftime('%Y-%m-%d')
    activity_by_hour = webui.get_activity_by_hour(date_str)
    top_species = webui.get_top_species(date_str)
    species_peak_hours = webui.get_species_peak_hours(date_str)
    total_detections = sum(
        item['total']
        for item in activity_by_hour
    )
    busiest_hour = max(
        activity_by_hour,
        key=lambda x: x['total'],
        default=None
    )

    return render_template(
        'activity.html',
        activity_by_hour=activity_by_hour,
        top_species=top_species,
        total_detections=total_detections,
        busiest_hour=busiest_hour,
        species_count=len(top_species),
        species_peak_hours=species_peak_hours,
        date=date_str
    )


@public_bp.route('/live')
def live_view():
    return render_template(
        'live_view.html'
    )
