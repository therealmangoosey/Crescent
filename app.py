from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import mysql.connector
from decimal import Decimal, ROUND_HALF_UP
import os
import json
from dotenv import load_dotenv
import requests
import logging
from werkzeug.security import generate_password_hash, check_password_hash
from ping3 import ping
import socket
from functools import wraps

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

paymenter_config = {
    'user': os.getenv('PAYMENTER_DB_USER'),
    'password': os.getenv('PAYMENTER_DB_PASSWORD'),
    'host': os.getenv('PAYMENTER_DB_HOST'),
    'database': os.getenv('PAYMENTER_DB_NAME')
}

pterodactyl_config = {
    'user': os.getenv('PTERODACTYL_DB_USER'),
    'password': os.getenv('PTERODACTYL_DB_PASSWORD'),
    'host': os.getenv('PTERODACTYL_DB_HOST'),
    'database': os.getenv('PTERODACTYL_DB_NAME')
}

COSTS_FILE = 'costs.json'
EXTRA_INCOME_FILE = 'extra_income.txt'
USERS_FILE = 'users.json'

def get_db_connection(config, connection_name=""):
    try:
        connection = mysql.connector.connect(
            user=config['user'],
            password=config['password'],
            host=config['host'],
            database=config['database'],
            autocommit=True
        )
        if connection.is_connected():
            logging.info(f"Successfully connected to {connection_name} database")
            return connection
    except mysql.connector.Error as err:
        logging.error(f"Error connecting to {connection_name} database: {err}")
        return None

def to_decimal(value):
    try:
        return Decimal(value)
    except (ValueError, TypeError, DecimalException):
        return None

def load_costs(file_path):
    machine_costs = {}
    misc_costs = {}
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r') as f:
                data = json.load(f)
                machine_costs = {k: Decimal(str(v)) for k, v in data.get('machine_costs', {}).items()}
                misc_costs = {k: Decimal(str(v)) for k, v in data.get('misc_costs', {}).items()}
        except FileNotFoundError:
            logging.info(f"Costs file {file_path} not found, creating a new one.")
            open(file_path, 'w').close()
        except json.JSONDecodeError:
            logging.error(f"Error decoding JSON from {file_path}. Starting with empty costs.")
    else:
        logging.info(f"Costs file {file_path} does not exist, creating a new one.")
        open(file_path, 'w').close()
    return machine_costs, misc_costs

def save_costs(file_path, machine_costs, misc_costs):
    data = {'machine_costs': {k: float(v) for k, v in machine_costs.items()},
            'misc_costs': {k: float(v) for k, v in misc_costs.items()}}
    try:
        with open(file_path, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving costs to {file_path}: {e}")

def get_pterodactyl_db_connection():
    try:
        connection = mysql.connector.connect(**pterodactyl_config)
        if connection.is_connected():
            logging.info("Successfully connected to Pterodactyl database")
            return connection
    except mysql.connector.Error as err:
        logging.error(f"Error connecting to Pterodactyl database: {err}")
    return None

def get_pterodactyl_nodes():
    pconn = get_pterodactyl_db_connection()
    if not pconn:
        logging.error("Failed to connect to Pterodactyl database")
        return {}

    pcur = pconn.cursor(dictionary=True)
    nodes = {}
    try:
        pcur.execute("SELECT id, name FROM nodes")
        results = pcur.fetchall()
        for node in results:
            nodes[str(node['id'])] = node['name']
    except Exception as e:
        logging.error(f"Error fetching nodes from Pterodactyl database: {e}")
    finally:
        if pconn and pconn.is_connected():
            pcur.close()
            pconn.close()
    return nodes

def load_extra_income(file_path):
    try:
        with open(file_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}

def save_extra_income(file_path, data):
    try:
        with open(file_path, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving extra income to {file_path}: {e}")

def load_users():
    try:
        with open('users.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}

def save_users(users):
    with open('users.json', 'w') as f:
        json.dump(users, f, indent=4)

def load_extra_income(file_path):
    extra_income_details = {}
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and ':' in line:
                        name, value = line.split(':', 1)
                        decimal_value = to_decimal(value.strip())
                        if decimal_value is not None:
                            extra_income_details[name.strip()] = decimal_value
                        else:
                            logging.warning(f"Skipping invalid extra income value: {value.strip()}")
        except Exception as e:
            logging.error(f"Error loading extra income from {file_path}: {e}")
    else:
        logging.info(f"Extra income file {file_path} does not exist, creating a new one.")
        try:
            open(file_path, 'w').close()
        except Exception as e:
            logging.error(f"Error creating extra income file: {e}")
    return extra_income_details


def save_extra_income(file_path, income_details):
    try:
        with open(file_path, 'w') as f:
            for name, value in income_details.items():
                f.write(f"{name}:{value}\n")
    except Exception as e:
        logging.error(f"Error saving extra income to {file_path}: {e}")

def load_users():
    try:
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_users(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=4)

def is_admin(user_email):
    users = load_users()
    user_data = users.get(user_email)
    return user_data and user_data.get('is_admin', False)

app.jinja_env.globals['is_admin'] = is_admin

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_email' not in session or not is_admin(session['user_email']):
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
def home():
    if 'user_email' in session:
        return redirect(url_for('dashboard_page'))
    else:
        return redirect(url_for('login_page'))

@app.route('/incomings')
def index():
    if 'user_email' not in session:
        return redirect(url_for('login_page'))

    pc = get_db_connection(paymenter_config, "Paymenter")
    if not pc:
        return render_template('incomings.html', error="Failed to connect to Paymenter database", email=session.get('user_email'))

    cur = pc.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM services WHERE status IN ('active', 'suspended')")
        services = cur.fetchall()

        cur.execute("SELECT id, email FROM users")
        users = cur.fetchall()
        user_email_map = {u['id']: u['email'] for u in users}

        pconn = get_db_connection(pterodactyl_config, "Pterodactyl")
        if not pconn:
            cur.close(); pc.close()
            return render_template('incomings.html', error="Failed to connect to Pterodactyl database", email=session.get('user_email'))

        pcur = pconn.cursor(dictionary=True)
        pcur.execute("SELECT id, name FROM nodes")
        node_map = {n['id']: n['name'] for n in pcur.fetchall()}

        node_income = {}
        service_details = {}
        unmatched_services = []
        unmatched_total = Decimal('0.00')

        for svc in services:
            price = to_decimal(svc['price'])
            if price is None:
                continue
            pcur.execute("SELECT uuid, name, node_id FROM servers WHERE external_id=%s",
                        (str(svc['id']),))
            srv = pcur.fetchone()
            email = user_email_map.get(svc['user_id'], 'Unknown')

            if srv:
                node_name = node_map.get(srv['node_id'], 'Unknown Node')
                node_income.setdefault(node_name, Decimal('0.00'))
                node_income[node_name] += price
                service_details.setdefault(node_name, []).append({
                    'service_id': svc['id'],
                    'user_email': email,
                    'price': f"{price:.2f}",
                    'server_uuid': srv['uuid'],
                    'server_name': srv['name'],
                    'status': svc['status'],
                })
            else:
                unmatched_services.append({
                    'service_id': svc['id'],
                    'user_email': email,
                    'price': f"{price:.2f}",
                    'status': svc['status'],
                })
                unmatched_total += price

        machine_costs, misc_costs = load_costs(COSTS_FILE)
        extra_income_details = load_extra_income(EXTRA_INCOME_FILE)
        extra_income = sum(extra_income_details.values(), Decimal('0.00'))

        total_cost = sum(machine_costs.values(), Decimal('0.00')) + sum(misc_costs.values(), Decimal('0.00'))
        total_income = sum(node_income.values(), Decimal('0.00')) + unmatched_total + extra_income
        profit = total_income - total_cost

        total_cost = total_cost.quantize(Decimal('0.01'), ROUND_HALF_UP)
        total_income = total_income.quantize(Decimal('0.01'), ROUND_HALF_UP)
        profit = profit.quantize(Decimal('0.01'), ROUND_HALF_UP)

        cur.close(); pc.close()
        pcur.close(); pconn.close()

        return render_template('incomings.html',
                                 node_income=node_income,
                                 service_details=service_details,
                                 unmatched_services=unmatched_services,
                                 unmatched_services_total=unmatched_total,
                                 total_cost=total_cost,
                                 total_income=total_income,
                                 profit=profit,
                                 machine_costs=machine_costs,
                                 cost_details=machine_costs,
                                 extra_income=extra_income,
                                 extra_income_details=extra_income_details,
                                 email=session.get('user_email')
                                 )
    except Exception as e:
        logging.error(f"Error in index route: {e}")
        return render_template('incomings.html', error="An error occurred while processing your request.", email=session.get('user_email'))
    finally:
        if pc and pc.is_connected():
            cur.close()
            pc.close()
        if pconn and pconn.is_connected():
            pcur.close()
            pconn.close()

@app.route('/outgoings', methods=['GET'])
def outgoings_page():
    if 'user_email' not in session:
        return redirect(url_for('login_page'))

    machine_costs, misc_costs = load_costs(COSTS_FILE)
    pterodactyl_nodes = get_pterodactyl_nodes()
    nodes_with_costs = {}

    orphaned_costs = set(machine_costs.keys()) - set(pterodactyl_nodes.keys())
    for orphaned_id in orphaned_costs:
        del machine_costs[orphaned_id]

    for node_id, node_name in pterodactyl_nodes.items():
        nodes_with_costs[node_id] = {'name': node_name, 'cost': machine_costs.get(node_id, Decimal('0.00'))}

    save_costs(COSTS_FILE, machine_costs, misc_costs)

    total_machine_outgoings = sum(cost['cost'] for cost in nodes_with_costs.values())
    total_misc_outgoings = sum(misc_costs.values()) if misc_costs else Decimal('0.00')
    total_outgoings = total_machine_outgoings + total_misc_outgoings

    return render_template('outgoings.html', machine_costs=nodes_with_costs, misc_costs=misc_costs,
                           total_outgoings=total_outgoings.quantize(Decimal('0.01')),
                           total_machine_outgoings=total_machine_outgoings.quantize(Decimal('0.01')),
                           total_misc_outgoings=total_misc_outgoings.quantize(Decimal('0.01')),
                           email=session.get('user_email'))

@app.route('/update_machine_cost', methods=['POST'])
def update_machine_cost():
    if 'user_email' not in session:
        return redirect(url_for('login_page'))

    node_id = request.form.get('node_id')
    cost_value = request.form.get('cost')
    if node_id and cost_value is not None:
        try:
            cost = Decimal(cost_value).quantize(Decimal('0.01'), 'ROUND_HALF_UP')
            machine_costs, misc_costs = load_costs(COSTS_FILE)
            machine_costs[node_id] = cost
            save_costs(COSTS_FILE, machine_costs, misc_costs)
            return redirect(url_for('outgoings_page'))
        except DecimalException:
            flash('Invalid cost value.', 'danger')
            return redirect(url_for('outgoings_page'))
    return redirect(url_for('outgoings_page'))

@app.route('/settings', methods=['GET'])
def settings_page():
    if 'user_email' not in session:
        return redirect(url_for('login_page'))
    users = load_users()
    user_data = users.get(session['user_email'])
    return render_template('settings.html', is_admin=is_admin, email=session.get('user_email'))


@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        users = load_users()

        if email in users and check_password_hash(users[email]['password'], password):
            session['user_email'] = email
            return redirect(url_for('dashboard_page'))

        else:
            return render_template('login.html', error='Invalid email or password')

    return render_template('login.html')

@app.route('/dashboard')
def dashboard_page():
    if 'user_email' not in session:
        return redirect(url_for('login_page'))

    pc = get_db_connection(paymenter_config, "Paymenter")
    if not pc:
        return render_template('dashboard.html', error="Failed to connect to Paymenter database", email=session.get('user_email'))

    cur = pc.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM services WHERE status IN ('active', 'suspended')")
        services = cur.fetchall()
        print("Services data:", services)

        cur.execute("SELECT id, email FROM users")
        users = cur.fetchall()
        user_email_map = {u['id']: u['email'] for u in users}

        pconn = get_db_connection(pterodactyl_config, "Pterodactyl")
        if not pconn:
            cur.close(); pc.close()
            return render_template('dashboard.html', error="Failed to connect to Pterodactyl database", email=session.get('user_email'))

        pcur = pconn.cursor(dictionary=True)
        pcur.execute("SELECT id, name, memory, disk, memory_overallocate, disk_overallocate FROM nodes")
        nodes_db_data = pcur.fetchall()
        node_info_map = {n['name']: {'memory': n['memory'], 'id': n['id'], 'disk': n['disk'],
                                     'memory_overallocate': n['memory_overallocate'],
                                     'disk_overallocate': n['disk_overallocate']} for n in nodes_db_data}
        node_name_to_id = {n['name']: n['id'] for n in nodes_db_data}
        print("Node info map:", node_info_map)

        pcur.execute("SELECT uuid, id, node_id, memory, disk FROM servers")
        servers_data = pcur.fetchall()
        print("Servers data:", servers_data)
        server_uuid_map = {s['id']: s['uuid'] for s in servers_data}
        server_node_map = {s['id']: s['node_id'] for s in servers_data}
        server_allocations = {s['id']: {'memory': s['memory'], 'disk': s['disk']} for s in servers_data}
        total_servers = len(server_uuid_map)

        node_id_to_name = {n['id']: n['name'] for n in nodes_db_data}
        print("Node ID to name map:", node_id_to_name)

        panel_url = os.getenv('PTERODACTYL_API_URL')
        api_key = os.getenv('PTERODACTYL_API_KEY')
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/vnd.pterodactyl.v1+json",
            "Content-Type": "application/json"
        }

        node_revenue = {}
        service_details = {}
        unmatched_services = []
        unmatched_total = Decimal('0.00')
        node_status = {}

        for svc in services:
            price = to_decimal(svc['price'])
            if price is None:
                continue
            pcur.execute("SELECT uuid, name, node_id, id FROM servers WHERE external_id=%s",
                        (str(svc['id']),))
            srv = pcur.fetchone()
            print(f"Server for service {svc['id']}:", srv)
            email = user_email_map.get(svc['user_id'], 'Unknown')

            if srv:
                node_name = node_id_to_name.get(srv['node_id'], 'Unknown Node')
                node_revenue.setdefault(node_name, Decimal('0.00'))
                node_revenue[node_name] += price
                service_details.setdefault(node_name, []).append({
                    'service_id': svc['id'],
                    'user_email': email,
                    'price': f"{price:.2f}",
                    'server_uuid': srv['uuid'],
                    'server_name': srv['name'],
                    'status': svc['status'],
                })
            else:
                unmatched_services.append({
                    'service_id': svc['id'],
                    'user_email': email,
                    'price': f"{price:.2f}",
                    'status': svc['status'],
                })
                unmatched_total += price

        machine_costs, misc_costs = load_costs(COSTS_FILE)
        extra_income_details = load_extra_income(EXTRA_INCOME_FILE)
        extra_income = sum(extra_income_details.values(), Decimal('0.00'))

        total_cost = sum(machine_costs.values(), Decimal('0.00')) + sum(misc_costs.values(), Decimal('0.00'))
        total_income = sum(node_revenue.values(), Decimal('0.00')) + unmatched_total + extra_income
        profit = total_income - total_cost

        total_cost = total_cost.quantize(Decimal('0.01'), ROUND_HALF_UP)
        total_income = total_income.quantize(Decimal('0.01'), ROUND_HALF_UP)
        profit = profit.quantize(Decimal('0.01'), ROUND_HALF_UP)

        top_nodes_with_info = []
        for node_name, revenue in sorted(node_revenue.items(), key=lambda item: item[1], reverse=True):
            node_info = node_info_map.get(node_name, {'memory': 0, 'id': None, 'disk': 0,
                                                     'memory_overallocate': 0, 'disk_overallocate': 0})
            memory_gb = node_info.get('memory', 0) / 1024
            node_id = node_info.get('id')
            wings_url = None
            wings_port = None
            is_online = False

            if node_id:
                panel_node_url = f"{panel_url}/api/application/nodes/{node_id}"
                try:
                    panel_response = requests.get(panel_node_url, headers=headers, timeout=5)
                    panel_response.raise_for_status()
                    node_data = panel_response.json().get('attributes', {})
                    wings_url = node_data.get('fqdn') or node_data.get('ip')
                    wings_port = node_data.get('daemon_listen')

                    if wings_url and wings_port is not None:
                        try:
                            sock = socket.create_connection((wings_url, int(wings_port)), timeout=1)
                            is_online = True
                            sock.close()
                            print(f"TCP connection to {wings_url}:{wings_port} for node {node_name}: Success")
                        except (socket.error, socket.timeout) as e:
                            is_online = False
                            print(f"TCP connection to {wings_url}:{wings_port} for node {node_name}: Failed ({e})")
                    elif wings_url:
                        delay = ping(wings_url, timeout=1)
                        if delay is not None:
                            is_online = True
                            print(f"Pinging {wings_url} (no port) for node {node_name}: Success")
                        else:
                            is_online = False
                            print(f"Pinging {wings_url} (no port) for node {node_name}: Failed")
                    else:
                        print(f"Could not find FQDN or IP for node {node_name}")

                except requests.exceptions.RequestException as e:
                    logging.warning(f"Failed to fetch Panel details for node {node_name} (ID: {node_id}): {e}")

            top_nodes_with_info.append({
                'name': node_name,
                'revenue': revenue,
                'memory': f"{memory_gb:.2f}",
                'online': is_online
            })
        print("Node revenue with info:", top_nodes_with_info)

        total_allocated_memory_mb = 0
        total_available_memory_mb = 0
        total_allocated_disk_mb = 0
        total_available_disk_mb = 0

        for node in nodes_db_data:
            node_id = node['id']
            node_physical_memory_mb = node['memory'] or 0
            node_memory_overallocate_percent = node['memory_overallocate'] or 0
            node_total_disk_mb = node['disk'] or 0
            node_disk_overallocate_percent = node['disk_overallocate'] or 0

            total_available_memory_mb += int(node_physical_memory_mb * (1 + (node_memory_overallocate_percent / 100)))
            total_available_disk_mb += int(node_total_disk_mb * (1 + (node_disk_overallocate_percent / 100)))

            for server in servers_data:
                if server['node_id'] == node_id:
                    total_allocated_memory_mb += server['memory'] or 0
                    total_allocated_disk_mb += server['disk'] or 0

        total_allocated_memory_gb = total_allocated_memory_mb / 1024
        total_available_memory_gb = total_available_memory_mb / 1024
        total_allocated_disk_gb = total_allocated_disk_mb / 1024
        total_available_disk_gb = total_available_disk_mb / 1024

        memory_percent = (total_allocated_memory_mb / total_available_memory_mb) * 100 if total_available_memory_mb > 0 else 0
        disk_percent = (total_allocated_disk_mb / total_available_disk_mb) * 100 if total_available_disk_mb > 0 else 0


        cur.close(); pc.close()
        pcur.close(); pconn.close()

        return render_template('dashboard.html',
                                 total_income=total_income,
                                 total_cost=total_cost,
                                 profit=profit,
                                 email=session['user_email'],
                                 total_servers=total_servers,
                                 top_nodes=top_nodes_with_info,
                                 total_allocated_memory_gb=f"{total_allocated_memory_gb:.2f}",
                                 total_available_memory_gb=f"{total_available_memory_gb:.2f}",
                                 memory_percent=float(f"{memory_percent:.2f}"),
                                 total_allocated_disk_gb=f"{total_allocated_disk_gb:.2f}",
                                 total_available_disk_gb=f"{total_available_disk_gb:.2f}",
                                 disk_percent=float(f"{disk_percent:.2f}")
                                 )
    except Exception as e:
        logging.error(f"Error in dashboard_page route: {e}")
        return render_template('dashboard.html',
                                 error="An error occurred while processing your request.",
                                 email=session.get('user_email'),
                                 total_income=0,
                                 total_cost=0,
                                 profit=0,
                                 total_servers=0,
                                 top_nodes=[],
                                 total_allocated_memory_gb=0,
                                 total_available_memory_gb=0,
                                 memory_percent=0,
                                 total_allocated_disk_gb=0,
                                 total_available_disk_gb=0,
                                 disk_percent=0
                                 )
    finally:
        if pc and pc.is_connected():
            cur.close()
            pc.close()
        if pconn and pconn.is_connected():
            pcur.close()
            pconn.close()

@app.route('/logout')
def logout():
    session.pop('user_email', None)
    return redirect(url_for('login_page'))

@app.route('/node_usage')
def node_usage():
    if 'user_email' not in session:
        return redirect(url_for('login_page'))

    pconn = get_db_connection(pterodactyl_config, "Pterodactyl")
    if not pconn:
        return render_template('node_usage.html', error="Failed to connect to the Pterodactyl database.", email=session.get('user_email'))

    pcur = pconn.cursor(dictionary=True)

    try:
        pcur.execute("""
            SELECT
                n.id AS node_id,
                n.name AS node_name,
                SUM(s.memory) AS total_allocated_memory_mb,
                SUM(s.disk) AS total_allocated_disk_mb,
                COUNT(s.id) AS server_count,
                n.memory AS node_physical_memory_mb,
                n.memory_overallocate AS node_memory_overallocate_percent,
                n.disk AS node_total_disk_mb,
                n.disk_overallocate AS node_disk_overallocate_percent
            FROM nodes n
            LEFT JOIN servers s ON n.id = s.node_id
            GROUP BY n.id
            ORDER BY n.name
        """)
        nodes_data = pcur.fetchall()

        nodes_usage = []

        for node in nodes_data:
            allocated_memory_mb = node['total_allocated_memory_mb'] or 0
            allocated_disk_mb = node['total_allocated_disk_mb'] or 0
            node_physical_memory_mb = node['node_physical_memory_mb'] or 0
            node_memory_overallocate_percent = node['node_memory_overallocate_percent'] or 0
            node_total_disk_mb = node['node_total_disk_mb'] or 0
            node_disk_overallocate_percent = node['node_disk_overallocate_percent'] or 0

            total_memory_for_display_mb = int(
                node_physical_memory_mb * (1 + (node_memory_overallocate_percent / 100)))
            total_disk_for_display_mb = int(
                node_total_disk_mb * (1 + (node_disk_overallocate_percent / 100)))

            allocated_memory_gb = allocated_memory_mb / 1024
            total_memory_for_display_gb = total_memory_for_display_mb / 1024
            allocated_disk_gb = allocated_disk_mb / 1024
            total_disk_for_display_gb = total_disk_for_display_mb / 1024

            memory_percent = (
                allocated_memory_mb / total_memory_for_display_mb) * 100 if total_memory_for_display_mb > 0 else 0
            disk_percent = (allocated_disk_mb /
                            total_disk_for_display_mb) * 100 if total_disk_for_display_gb > 0 else 0
            node_name = node['node_name']


            nodes_usage.append({
                'node_name': node_name,
                'total_allocated_memory_gb': f"{allocated_memory_gb:.2f}",
                'allocated_memory_percent': float(f"{memory_percent:.2f}"),
                'total_allocated_disk_gb': f"{allocated_disk_gb:.2f}",
                'allocated_disk_percent': float(f"{disk_percent:.2f}"),
                'server_count': node['server_count'],
                'total_memory_for_display_gb': f"{total_memory_for_display_gb:.2f}",
                'total_disk_for_display_gb': f"{total_disk_for_display_gb:.2f}",
            })

        pcur.close();
        pconn.close()
        return render_template('node_usage.html', nodes_usage=nodes_usage, email=session.get('user_email'))
    except Exception as e:
        logging.error(f"Error in node_usage route: {e}")
        return render_template('node_usage.html', error="An error occurred while processing your request.", email=session.get('user_email'))
    finally:
        if pconn and pconn.is_connected():
            pcur.close()
            pconn.close()

@app.route('/add_cost_ajax', methods=['POST'])
def add_cost_ajax():
    data = request.get_json()
    name = data.get('name')
    value = data.get('value')
    if name and value:
        decimal_value = to_decimal(value)
        if decimal_value is not None:
            machine_costs, misc_costs = load_costs(COSTS_FILE)
            misc_costs[name] = decimal_value
            save_costs(COSTS_FILE, machine_costs, misc_costs)
            return jsonify({'success': True, 'name': name, 'value': str(decimal_value)})
        else:
            return jsonify({'success': False, 'error': 'Invalid value'}), 400
    else:
        return jsonify({'success': False, 'error': 'Missing data'}), 400

@app.route('/remove_cost_ajax', methods=['POST'])
def remove_cost_ajax():
    data = request.get_json()
    name = data.get('name')
    if name and not name.startswith("NODE_"):
        machine_costs, misc_costs = load_costs(COSTS_FILE)
        if name in misc_costs:
            del misc_costs[name]
            save_costs(COSTS_FILE, machine_costs, misc_costs)
            return jsonify({'success': True, 'name': name})
        else:
            return jsonify({'success': False, 'error': 'Cost not found'}), 404
    else:
        return jsonify({'success': False, 'error': 'Invalid or missing name'}), 400

@app.route('/update_misc_cost_ajax', methods=['POST'])
def update_misc_cost_ajax():
    data = request.get_json()
    original_name = data.get('original_name')
    new_name = data.get('new_name')
    new_value = data.get('new_value')

    if original_name and new_name and new_value and not original_name.startswith("NODE_"):
        machine_costs, misc_costs = load_costs(COSTS_FILE)
        if original_name in misc_costs:
            decimal_value = to_decimal(new_value)
            if decimal_value is not None:
                cost = misc_costs.pop(original_name)
                misc_costs[new_name] = decimal_value
                save_costs(COSTS_FILE, machine_costs, misc_costs)
                return jsonify({'success': True, 'old_name': original_name, 'new_name': new_name, 'new_value': str(decimal_value)})
            else:
                return jsonify({'success': False, 'error': 'Invalid value'}), 400
        else:
            return jsonify({'success': False, 'error': 'Cost not found'}), 404
    else:
        return jsonify({'success': False, 'error': 'Missing or invalid data'}), 400

@app.route('/add_extra_income_ajax', methods=['POST'])
def add_extra_income_ajax():
    data = request.get_json()
    name = data.get('name')
    value = data.get('value')
    if name and value:
        decimal_value = to_decimal(value)
        if decimal_value is not None:
            extra_income_details = load_extra_income(EXTRA_INCOME_FILE)
            extra_income_details[name] = decimal_value
            save_extra_income(EXTRA_INCOME_FILE, extra_income_details)
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Invalid value'}), 400
    else:
        return jsonify({'success': False, 'error': 'Missing data'}), 400


@app.route('/remove_extra_income_ajax', methods=['POST'])
def remove_extra_income_ajax():
    data = request.get_json()
    name = data.get('name')
    if name:
        extra_income_details = load_extra_income(EXTRA_INCOME_FILE)
        if name in extra_income_details:
            del extra_income_details[name]
            save_extra_income(EXTRA_INCOME_FILE, extra_income_details)
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Income source not found'}), 404
    else:
        return jsonify({'success': False, 'error': 'Missing data'}), 400

@app.route('/update_income', methods=['POST'])
def update_income():
    data = request.get_json()
    original_name = data.get('original_name')
    new_name = data.get('new_name')
    new_value = data.get('new_value')

    if original_name is not None and new_name is not None and new_value is not None:
        income_details = load_extra_income(EXTRA_INCOME_FILE)
        if original_name in income_details:
            decimal_value = to_decimal(new_value)
            if decimal_value is not None:
                del income_details[original_name]
                income_details[new_name] = decimal_value
                save_extra_income(EXTRA_INCOME_FILE, income_details)
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'Invalid value provided'}), 400
        else:
            return jsonify({'success': False, 'error': 'Income source not found'}), 404
    else:
        return jsonify({'success': False, 'error': 'Missing data'}), 400
    
@app.route('/update_password', methods=['POST'])
def update_password():
    if 'user_email' not in session:
        return redirect(url_for('login_page'))

    users = load_users()
    user_data = users.get(session['user_email'])

    if not user_data:
        return render_template('settings.html', password_error='User not found.')

    old_password = request.form['old_password']
    new_password = request.form['new_password']
    confirm_password = request.form['confirm_password']

    if not check_password_hash(user_data['password'], old_password):
        return render_template('settings.html', password_error='Incorrect old password.')

    if new_password != confirm_password:
        return render_template('settings.html', password_error='New passwords do not match.')

    if old_password == new_password:
        return render_template('settings.html', password_error='New password cannot be the same as the old password.')

    hashed_password = generate_password_hash(new_password)
    users[session['user_email']]['password'] = hashed_password
    save_users(users)
    return render_template('settings.html', password_success='Password updated successfully.')

@app.route('/update_email', methods=['POST'])
def update_email():
    if 'user_email' not in session:
        return redirect(url_for('login_page'))

    users = load_users()
    user_data = users.get(session['user_email'])

    if not user_data:
        return render_template('settings.html', email_error='User not found.')

    current_password = request.form['current_password']
    new_email = request.form['new_email']

    if not check_password_hash(user_data['password'], current_password):
        return render_template('settings.html', email_error='Incorrect password.')

    if new_email == session['user_email']:
        return render_template('settings.html', email_error='New email cannot be the same as the current email.')

    if new_email in users:
        return render_template('settings.html', email_error='This email address is already in use.')

    users[new_email] = users.pop(session['user_email'])
    session['user_email'] = new_email
    save_users(users)
    return render_template('settings.html', email_success='Email updated successfully.')

@app.route('/admin')
@admin_required
def admin_page():
    users = load_users()
    return render_template('admin.html', users=users, email=session.get('user_email'))

@app.route('/admin/add_user', methods=['POST'])
@admin_required
def add_user():
    email = request.form['email']
    password = request.form['password']
    is_admin_checkbox = request.form.get('is_admin')

    if not email or not password:
        return redirect(url_for('admin_page'))
    users = load_users()
    if email in users:
        return redirect(url_for('admin_page'))
    else:
        hashed_password = generate_password_hash(password)
        is_admin_status = True if is_admin_checkbox else False
        users[email] = {'password': hashed_password, 'is_admin': is_admin_status}
        save_users(users)
    return redirect(url_for('admin_page'))

@app.route('/admin/update_password', methods=['POST'])
@admin_required
def admin_update_password():
    email_to_update = request.form.get('email')
    new_password = request.form['password']
    if not new_password:
        return redirect(url_for('admin_page'))
    users = load_users()
    if email_to_update in users:
        hashed_password = generate_password_hash(new_password)
        users[email_to_update]['password'] = hashed_password
        save_users(users)
    else:
        pass
    return redirect(url_for('admin_page'))

@app.route('/admin/make_admin', methods=['POST'])
@admin_required
def make_admin():
    email_to_admin = request.form.get('email')
    users = load_users()
    if email_to_admin in users:
        users[email_to_admin]['is_admin'] = not users[email_to_admin].get('is_admin', False)
        save_users(users)
    else:
        pass
    return redirect(url_for('admin_page'))

@app.route('/admin/remove_user', methods=['POST'])
@admin_required
def remove_user():
    email_to_remove = request.form.get('email')
    if email_to_remove == session.get('user_email'):
        return redirect(url_for('admin_page'))
    users = load_users()
    if email_to_remove in users:
        del users[email_to_remove]
        save_users(users)
    else:
        pass
    return redirect(url_for('admin_page'))

if __name__ == '__main__':
    if not os.path.exists(USERS_FILE) or os.stat(USERS_FILE).st_size == 0:
        default_admin_email = os.getenv('DEFAULT_ADMIN_EMAIL')
        default_admin_password = os.getenv('DEFAULT_ADMIN_PASSWORD')

        if default_admin_email and default_admin_password:
            hashed_password = generate_password_hash(default_admin_password)
            default_users = {
                default_admin_email: {'password': hashed_password, 'is_admin': True}
            }
            save_users(default_users)
            print(f"Created default 'users.json' with an admin user from .env.")
        else:
            print("Warning: DEFAULT_ADMIN_EMAIL and/or DEFAULT_ADMIN_PASSWORD not set in .env. No default admin user created.")

    app.run(debug=True, host='127.0.0.1')