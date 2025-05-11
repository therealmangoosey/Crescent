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
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_extra_income(file_path, data):
    try:
        with open(file_path, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving extra income to {file_path}: {e}")

def load_users():
    try:
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
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
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_email' not in session or not is_admin(session['user_email']):
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function

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
