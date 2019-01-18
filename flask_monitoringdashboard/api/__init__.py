import datetime
import numpy

from flask import jsonify, request, json

from flask_monitoringdashboard import blueprint, user_app, config
from flask_monitoringdashboard.core.auth import secure, admin_secure
from flask_monitoringdashboard.core.colors import get_color
from flask_monitoringdashboard.core.measurement import add_decorator
from flask_monitoringdashboard.core.timezone import to_local_datetime, to_utc_datetime
from flask_monitoringdashboard.core.utils import get_details, get_endpoint_details, simplify
from flask_monitoringdashboard.database import Request, session_scope, row2dict
from flask_monitoringdashboard.database.count_group import count_requests_group, get_value, count_requests_per_day
from flask_monitoringdashboard.database.data_grouped import get_endpoint_data_grouped
from flask_monitoringdashboard.database.endpoint import get_last_requested, get_endpoints, update_endpoint, \
    get_endpoint_by_name, get_num_requests
from flask_monitoringdashboard.database.versions import get_versions


@blueprint.route('/api/info')
@secure
def get_info():
    with session_scope() as db_session:
        return jsonify(get_details(db_session))


@blueprint.route('/api/overview')
@secure
def get_overview():
    week_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    now_local = to_local_datetime(datetime.datetime.utcnow())
    today_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_utc = to_utc_datetime(today_local)

    result = []
    with session_scope() as db_session:
        from numpy import median

        hits_today = count_requests_group(db_session, Request.time_requested > today_utc)
        hits_week = count_requests_group(db_session, Request.time_requested > week_ago)
        hits = count_requests_group(db_session)

        median_today = get_endpoint_data_grouped(db_session, median, Request.time_requested > today_utc)
        median_week = get_endpoint_data_grouped(db_session, median, Request.time_requested > week_ago)
        median = get_endpoint_data_grouped(db_session, median)
        access_times = get_last_requested(db_session)

        for endpoint in get_endpoints(db_session):
            result.append({
                'id': endpoint.id,
                'name': endpoint.name,
                'monitor': endpoint.monitor_level,
                'color': get_color(endpoint.name),
                'hits-today': get_value(hits_today, endpoint.id),
                'hits-week': get_value(hits_week, endpoint.id),
                'hits-overall': get_value(hits, endpoint.id),
                'median-today': get_value(median_today, endpoint.id),
                'median-week': get_value(median_week, endpoint.id),
                'median-overall': get_value(median, endpoint.id),
                'last-accessed': get_value(access_times, endpoint.name, default=None)
            })
    return jsonify(result)


@blueprint.route('/api/versions')
@secure
def versions():
    with session_scope() as db_session:
        return jsonify(get_versions(db_session))


@blueprint.route('/api/endpoints')
@secure
def endpoints():
    with session_scope() as db_session:
        return jsonify([row2dict(row) for row in get_endpoints(db_session)])


@blueprint.route('api/multi_version', methods=['POST'])
@secure
def multi_version():
    data = json.loads(request.data)['data']
    endpoints = data['endpoints']
    versions = data['versions']

    with session_scope() as db_session:
        endpoints = [get_endpoint_by_name(db_session, name) for name in endpoints]
        requests = [count_requests_group(db_session, Request.version_requested == v) for v in versions]

        total_hits = numpy.zeros(len(versions))
        hits = numpy.zeros((len(endpoints), len(versions)))

        for i, _ in enumerate(versions):
            total_hits[i] = max(1, sum([value for key, value in requests[i]]))

        for j, _ in enumerate(endpoints):
            for i, _ in enumerate(versions):
                hits[j][i] = get_value(requests[i], endpoints[j].id) * 100 / total_hits[i]
        return jsonify(hits.tolist())


@blueprint.route('/api/requests/<start_date>/<end_date>')
@secure
def num_requests(start_date, end_date):
    with session_scope() as db_session:
        start_date = datetime.datetime.strptime(start_date, '%Y-%m-%d')
        end_date = datetime.datetime.strptime(end_date, '%Y-%m-%d')
        numdays = (end_date - start_date).days + 1
        days = [start_date + datetime.timedelta(days=i) for i in range(numdays)]

        hits = count_requests_per_day(db_session, days)
        endpoints = get_endpoints(db_session)
        data = [{
            'name': end.name,
            'values': [get_value(hits_day, end.id) for hits_day in hits]
        } for end in endpoints]

        return jsonify({
            'days': [d.strftime('%Y-%m-%d') for d in days],
            'data': data
        })


@blueprint.route('/api/api_performance', methods=['POST'])
@secure
def api_performance():
    data = json.loads(request.data)['data']
    endpoints = data['endpoints']

    with session_scope() as db_session:
        db_endpoints = [get_endpoint_by_name(db_session, end) for end in endpoints]
        data = get_endpoint_data_grouped(db_session, lambda x: simplify(x, 10))
        return jsonify([{
            'name': end.name,
            'values': get_value(data, end.id, default=[])
        } for end in db_endpoints])


@blueprint.route('/api/set_rule', methods=['POST'])
@admin_secure
def set_rule():
    """
    the data from the form is validated and processed, such that the required rule is monitored
    """
    endpoint_name = request.form['name']
    value = int(request.form['value'])
    with session_scope() as db_session:
        update_endpoint(db_session, endpoint_name, value=value)

        # Remove wrapper
        original = getattr(user_app.view_functions[endpoint_name], 'original', None)
        if original:
            user_app.view_functions[endpoint_name] = original

    with session_scope() as db_session:
        add_decorator(get_endpoint_by_name(db_session, endpoint_name))

    return 'OK'


@blueprint.route('api/deploy_details')
@secure
def deploy_details():
    with session_scope() as db_session:
        details = get_details(db_session)
    details['first-request'] = to_local_datetime(datetime.datetime.fromtimestamp(details['first-request']))
    details['first-request-version'] = to_local_datetime(datetime.datetime.
                                                         fromtimestamp(details['first-request-version']))
    return jsonify(details)


@blueprint.route('api/deploy_config')
@secure
def deploy_config():
    return jsonify({
        'database_name': config.database_name,
        'username': config.username,
        'guest_username': config.guest_username,
        'outlier_detection_constant': config.outlier_detection_constant,
        'timezone': str(config.timezone),
        'colors': config.colors
    })


@blueprint.route('api/endpoint_info/<endpoint_id>')
@secure
def endpoint_info(endpoint_id):
    with session_scope() as db_session:
        return jsonify(get_endpoint_details(db_session, endpoint_id))


@blueprint.route('api/hourly_load/<start_date>/<end_date>')
@blueprint.route('api/hourly_load/<start_date>/<end_date>/<endpoint_id>')
@secure
# both days must be in the form: yyyy-mm-dd
def hourly_load(start_date, end_date, endpoint_id=None):
    start_date = datetime.datetime.strptime(start_date, '%Y-%m-%d')
    end_date = datetime.datetime.strptime(end_date, '%Y-%m-%d')
    numdays = (end_date - start_date).days + 1

    # list of hours: 0:00 - 23:00
    hours = ['0{}:00'.format(h) for h in range(0, 10)] + ['{}:00'.format(h) for h in range(10, 24)]
    heatmap_data = numpy.zeros((len(hours), numdays))

    start_datetime = to_utc_datetime(datetime.datetime.combine(start_date, datetime.time(0, 0, 0, 0)))
    end_datetime = to_utc_datetime(datetime.datetime.combine(end_date, datetime.time(23, 59, 59)))

    with session_scope() as db_session:
        for time, count in get_num_requests(db_session, endpoint_id, start_datetime, end_datetime):
            parsed_time = datetime.datetime.strptime(time, '%Y-%m-%d %H:%M:%S')
            day_index = (parsed_time - start_datetime).days
            hour_index = int(to_local_datetime(parsed_time).strftime('%H'))
            heatmap_data[hour_index][day_index] = count
    return jsonify({
        'days': [(start_date + datetime.timedelta(days=i)).strftime('%Y-%m-%d') for i in range(numdays)],
        "data": heatmap_data.tolist()
    })
