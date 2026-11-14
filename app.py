import eventlet
eventlet.monkey_patch()  # Necessary for Flask-SocketIO with eventlet

from datetime import timedelta, datetime
import sqlite3
import os
import random
import base64
from functools import wraps

import cv2
import numpy as np
import pandas as pd
from flask import (Flask, render_template, request, jsonify, session,
                   redirect, url_for, flash)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import generate_password_hash, check_password_hash

# =====================================================================
# SETTINGS
# =====================================================================
# Admin face verification is OFF by default. Admin logs in with email +
# password only. To turn it on later, set the environment variable
# FACE_VERIFICATION=1 (and make sure your face-api.js models load).
FACE_VERIFICATION_ENABLED = os.environ.get('FACE_VERIFICATION', '0') == '1'

DB_FILE = 'college_exam_database.db'
UPLOAD_FOLDER = 'uploads'
BRANCH_NAMES = {"comps": "Computer", "csd": "CS & Design", "aids": "AI/DS",
                "mech": "Mechanical", "civil": "Civil"}

# =====================================================================
# AI MODEL SETUP (YOLO) - needs OpenCV 4.x (Darknet removed in OpenCV 5)
# =====================================================================
yolo_path = "yolo_model"
net = None
classes = []
output_layers = []
ai_model_loaded = False

try:
    weights_path = os.path.join(yolo_path, "yolov3-tiny.weights")
    cfg_path = os.path.join(yolo_path, "yolov3-tiny.cfg")
    names_path = os.path.join(yolo_path, "coco.names")

    if all(os.path.exists(p) for p in (weights_path, cfg_path, names_path)):
        net = cv2.dnn.readNet(weights_path, cfg_path)
        layer_names = net.getLayerNames()
        unconnected = np.array(net.getUnconnectedOutLayers()).flatten()
        output_layers = [layer_names[int(i) - 1] for i in unconnected]
        with open(names_path, "r") as f:
            classes = [line.strip() for line in f.readlines()]
        ai_model_loaded = True
        print("AI Model loaded successfully.")
    else:
        print(f"AI Model files not found in '{yolo_path}'. AI proctoring disabled.")
except Exception as e:
    print(f"Error loading AI Model: {e}. AI proctoring disabled.")
    net = None

# =====================================================================
# APP SETUP
# =====================================================================
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'kEshU-RAnDoM-sTr1nG-f0R-s3ss10n-s3cur1tY')  # CHANGE THIS!
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

socketio = SocketIO(app, async_mode='eventlet')

# Limits are generous: the old 50/hour default would block students mid-exam.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["5000 per day", "1000 per hour"],
    storage_uri="memory://"
)

# =====================================================================
# DATABASE
# =====================================================================
def add_column_if_not_exists(cursor, table_name, column_name, column_type):
    """Adds a column to an existing table if it is missing."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    existing = [row[1] for row in cursor.fetchall()]
    if column_name not in existing:
        print(f"Adding column '{column_name}' to '{table_name}'...")
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Users table (single, complete definition)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            roll_no TEXT UNIQUE,
            branch_code TEXT,
            year INTEGER,
            role TEXT NOT NULL DEFAULT 'student' CHECK(role IN ('student', 'admin')),
            is_active INTEGER NOT NULL DEFAULT 1,
            secret_question TEXT,
            secret_answer_hash TEXT,
            face_descriptor TEXT
        )
    ''')
    # Upgrade databases created with older versions of the table
    add_column_if_not_exists(cursor, 'users', 'is_active', 'INTEGER NOT NULL DEFAULT 1')
    add_column_if_not_exists(cursor, 'users', 'secret_question', 'TEXT')
    add_column_if_not_exists(cursor, 'users', 'secret_answer_hash', 'TEXT')
    add_column_if_not_exists(cursor, 'users', 'face_descriptor', 'TEXT')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            code TEXT UNIQUE NOT NULL,
            branch_code TEXT NOT NULL,
            year INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        )
    ''')
    add_column_if_not_exists(cursor, 'subjects', 'is_active', 'INTEGER NOT NULL DEFAULT 1')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,
            option1 TEXT NOT NULL, option2 TEXT NOT NULL, option3 TEXT NOT NULL, option4 TEXT NOT NULL,
            correct_answer TEXT NOT NULL, subject_id INTEGER NOT NULL,
            FOREIGN KEY (subject_id) REFERENCES subjects (id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT, student_roll_no TEXT NOT NULL, subject_code TEXT NOT NULL,
            score INTEGER NOT NULL, total INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('Completed', 'Terminated')),
            reason TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_roll_no) REFERENCES users (roll_no) ON DELETE CASCADE
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS proctoring_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, student_roll_no TEXT NOT NULL, event_type TEXT NOT NULL,
            message TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_roll_no) REFERENCES users (roll_no) ON DELETE CASCADE
        )
    ''')

    # Default admin (created once)
    cursor.execute("SELECT id FROM users WHERE email = 'admin@nhitm.ac.in'")
    if cursor.fetchone() is None:
        cursor.execute("""
            INSERT INTO users (email, password_hash, name, role, is_active, secret_question, secret_answer_hash)
            VALUES (?, ?, ?, 'admin', 1, ?, ?)
        """, ('admin@nhitm.ac.in', generate_password_hash('admin'), 'Admin',
              "What is your favorite subject?", generate_password_hash('DefaultAnswer')))
        print("Default admin created (admin@nhitm.ac.in / admin). PLEASE CHANGE THE PASSWORD.")

    conn.commit()
    conn.close()
    print("Database initialized.")


# =====================================================================
# HELPERS
# =====================================================================
ACTIVE_EXAMS = {}        # {roll_no_str: {info}}
connected_students = {}  # {sid: roll_no_str}


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def log_proctoring_event(roll_no, event_type, message):
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("INSERT INTO proctoring_logs (student_roll_no, event_type, message) VALUES (?, ?, ?)",
                     (str(roll_no), event_type, message))
        conn.commit()
    except sqlite3.Error as e:
        print(f"DB Log Error ({roll_no}): {e}")
    finally:
        if conn:
            conn.close()


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin' not in session:
            flash('Admin access required.', 'error')
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function


def finish_admin_login(user_id, name):
    """Sets the final admin session."""
    session.clear()
    session['admin'] = True
    session['admin_name'] = name
    session['admin_id'] = user_id
    session.permanent = True


def start_admin_login(user):
    """
    Called after an admin's password has been checked.
    - Face verification OFF -> straight to the dashboard.
    - Face verification ON and a face is registered -> go to face check.
    - Face verification ON but no face registered -> log in and send to the
      profile page to register one (instead of locking the admin out).
    """
    has_face = 'face_descriptor' in user.keys() and bool(user['face_descriptor'])

    if FACE_VERIFICATION_ENABLED and has_face:
        session.clear()
        session['admin_pending_face_verification'] = user['id']
        session['admin_temp_name'] = user['name']
        return redirect(url_for('admin_verify_face'))

    finish_admin_login(user['id'], user['name'])
    if FACE_VERIFICATION_ENABLED and not has_face:
        flash('Please register your face to enable face verification.', 'info')
        return redirect(url_for('admin_profile'))
    return redirect(url_for('admin_dashboard'))


# =====================================================================
# GENERAL / LOGIN ROUTES
# =====================================================================
@app.route('/')
def login_page():
    if 'admin' in session:
        return redirect(url_for('admin_dashboard'))
    if 'email' in session:
        return redirect(url_for('student_dashboard'))
    return render_template('login.html', error=None)


@app.route('/login', methods=['POST'])
def handle_login():
    email = request.form.get('username')
    password = request.form.get('password')
    if not email or not password:
        flash('Email/password required.', 'error')
        return render_template('login.html', error='Email/password required.')

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()

    if not (user and check_password_hash(user['password_hash'], password)):
        flash('Invalid credentials.', 'error')
        return render_template('login.html', error='Invalid Credentials')

    if user['is_active'] == 0:
        flash('Your account is inactive. Please contact the administrator.', 'error')
        return render_template('login.html', error='Account inactive.')

    if user['role'] == 'admin':
        return start_admin_login(user)

    if user['role'] == 'student':
        if user['branch_code'] and user['year']:
            session.clear()
            session.permanent = True
            session['email'] = user['email']
            session['student_name'] = user['name']
            session['roll_no'] = user['roll_no']
            session['branch_code'] = user['branch_code']
            session['year'] = user['year']
            return redirect(url_for('student_dashboard'))
        flash('Profile incomplete. Cannot log in.', 'error')
        return render_template('login.html', error='Profile incomplete.')

    flash('Unknown user role.', 'error')
    return render_template('login.html', error='Unknown role.')


@app.route('/logout')
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for('login_page'))


# =====================================================================
# ADMIN LOGIN / FACE ROUTES
# =====================================================================
@app.route('/admin')
def admin_login_page():
    if 'admin' in session:
        return redirect(url_for('admin_dashboard'))
    return render_template('admin_login.html')


@app.route('/admin/login', methods=['POST'])
def handle_admin_login():
    email = request.form.get('username')
    password = request.form.get('password')
    if not email or not password:
        flash('Email and password required.', 'error')
        return redirect(url_for('admin_login_page'))

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email = ? AND role = 'admin'", (email,)).fetchone()
    conn.close()

    if user and check_password_hash(user['password_hash'], password):
        if user['is_active'] == 0:
            flash('Admin account is inactive.', 'error')
            return redirect(url_for('admin_login_page'))
        return start_admin_login(user)

    flash('Invalid admin credentials.', 'error')
    return redirect(url_for('admin_login_page'))


@app.route('/admin/verify_face', methods=['GET'])
@limiter.limit("10 per minute")
def admin_verify_face():
    admin_id = session.get('admin_pending_face_verification')
    if not admin_id:
        flash('Please log in with password first.', 'error')
        return redirect(url_for('admin_login_page'))

    conn = get_db()
    row = conn.execute("SELECT name, face_descriptor FROM users WHERE id = ?", (admin_id,)).fetchone()
    conn.close()

    if not row or not row['face_descriptor']:
        # No face registered: don't lock the admin out
        finish_admin_login(admin_id, row['name'] if row else 'Admin')
        flash('No face registered yet. Please register your face.', 'info')
        return redirect(url_for('admin_profile'))

    return render_template('admin_verify_face.html',
                           admin_name=session.get('admin_temp_name', 'Admin'),
                           stored_descriptor=row['face_descriptor'])


@app.route('/admin/complete_verification', methods=['POST'])
@limiter.limit("5 per minute")
def handle_admin_complete_verification():
    temp_admin_id = session.get('admin_pending_face_verification')
    if not temp_admin_id:
        return jsonify({'success': False, 'error': 'Invalid session',
                        'redirect_url': url_for('admin_login_page')}), 400

    conn = get_db()
    admin_user = conn.execute("SELECT id, name FROM users WHERE id = ? AND role = 'admin'",
                              (temp_admin_id,)).fetchone()
    conn.close()

    if not admin_user:
        session.clear()
        return jsonify({'success': False, 'error': 'User not found',
                        'redirect_url': url_for('admin_login_page')}), 404

    finish_admin_login(admin_user['id'], admin_user['name'])
    print(f"Admin {admin_user['id']} fully verified.")
    return jsonify({'success': True, 'redirect_url': url_for('admin_dashboard')})


@app.route('/admin/profile')
@admin_required
def admin_profile():
    face_registered = False
    admin_id = session.get('admin_id')
    if admin_id:
        conn = get_db()
        row = conn.execute("SELECT face_descriptor FROM users WHERE id = ?", (admin_id,)).fetchone()
        conn.close()
        face_registered = bool(row and row['face_descriptor'])
    return render_template('admin_profile.html', face_registered=face_registered)


@app.route('/admin/register_face', methods=['POST'])
def handle_admin_register_face():
    admin_id = session.get('admin_id') or session.get('admin_pending_face_verification')
    if not admin_id:
        return jsonify({'success': False, 'error': 'Invalid session'}), 400

    data = request.get_json(silent=True) or {}
    descriptor_str = data.get('descriptor')
    if not descriptor_str:
        return jsonify({'success': False, 'error': 'No descriptor received'}), 400

    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("UPDATE users SET face_descriptor = ? WHERE id = ?", (descriptor_str, admin_id))
        conn.commit()
        flash('Face registered successfully!', 'success')
        return jsonify({'success': True})
    except sqlite3.Error as e:
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'error': f'Database error: {e}'}), 500
    finally:
        if conn:
            conn.close()


# =====================================================================
# STUDENT ROUTES
# =====================================================================
@app.route('/student_dashboard')
def student_dashboard():
    if 'email' not in session or 'roll_no' not in session:
        return redirect(url_for('login_page'))
    branch_code, year = session.get('branch_code'), session.get('year')
    conn = get_db()
    subjects_db = conn.execute(
        "SELECT name, code FROM subjects WHERE branch_code = ? AND year = ? AND is_active = 1",
        (branch_code, year)).fetchall()
    conn.close()
    available_subjects = {s['code']: {'name': s['name']} for s in subjects_db}
    return render_template('student_dashboard.html',
                           student_name=session.get('student_name'),
                           branch_name=BRANCH_NAMES.get(branch_code, "N/A"),
                           branch_code=branch_code, year=year, subjects=available_subjects)


@app.route('/student/results')
def student_results():
    if 'email' not in session or 'roll_no' not in session:
        return redirect(url_for('login_page'))
    conn = get_db()
    results_list = conn.execute(
        "SELECT r.*, s.name as subject_name FROM results r JOIN subjects s ON r.subject_code = s.code "
        "WHERE r.student_roll_no = ? ORDER BY r.timestamp DESC", (session['roll_no'],)).fetchall()
    conn.close()
    return render_template('student_results.html', results=results_list,
                           student_name=session.get('student_name'))


# =====================================================================
# EXAM FLOW ROUTES
# =====================================================================
@app.route('/exam_notice/<subject_code>')
def exam_notice(subject_code):
    if 'email' not in session:
        return redirect(url_for('login_page'))
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("SELECT name FROM subjects WHERE code = ?", (subject_code,)).fetchone()
    conn.close()
    if not row:
        return "Subject not found", 404
    return render_template('exam_notice.html', subject_code=subject_code, subject_name=row[0])


@app.route('/exam/<subject_code>')
def exam_page(subject_code):
    if 'email' not in session or 'roll_no' not in session:
        return redirect(url_for('login_page'))
    roll_no_str = str(session['roll_no'])
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM results WHERE student_roll_no = ? AND subject_code = ?",
                   (roll_no_str, subject_code))
    if cursor.fetchone():
        conn.close()
        flash("Already submitted.", "info")
        return render_template('exam_ended.html', message="Already submitted.")
    cursor.execute("SELECT name FROM subjects WHERE code = ?", (subject_code,))
    subject_row = cursor.fetchone()
    conn.close()
    if not subject_row:
        flash("Subject not found.", "error")
        return redirect(url_for('student_dashboard'))

    ACTIVE_EXAMS[roll_no_str] = {'name': session['student_name'], 'subject': subject_code,
                                 'startTime': datetime.now().timestamp()}
    print(f"SERVER: Added {roll_no_str} to ACTIVE_EXAMS. Current: {list(ACTIVE_EXAMS.keys())}")
    return render_template('exam.html', subject=subject_code, subject_name=subject_row[0],
                           roll_no=roll_no_str)


@app.route('/submission-success')
def submission_success():
    if 'email' not in session:
        return redirect(url_for('login_page'))
    return render_template('submission_success.html', student_name=session.get('student_name'))


@app.route('/exam-terminated')
def exam_terminated():
    if 'email' not in session:
        return redirect(url_for('login_page'))
    return render_template('exam_terminated.html', student_name=session.get('student_name'))


# =====================================================================
# ADMIN ROUTES
# =====================================================================
@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    return render_template('admin_dashboard.html')


@app.route('/admin/students')
@admin_required
def admin_manage_students():
    conn = get_db()
    students = conn.execute(
        "SELECT id, name, roll_no, email, branch_code, year, is_active FROM users "
        "WHERE role = 'student' ORDER BY name").fetchall()
    conn.close()
    return render_template('admin_students.html', students=students, branches=BRANCH_NAMES)


@app.route('/admin/students/toggle_status/<int:student_id>', methods=['POST'])
@admin_required
def admin_toggle_student_status(student_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        row = conn.execute("SELECT is_active FROM users WHERE id = ? AND role = 'student'",
                           (student_id,)).fetchone()
        if not row:
            flash('Student not found.', 'error')
            return redirect(url_for('admin_manage_students'))
        new_status = 0 if row[0] == 1 else 1
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_status, student_id))
        conn.commit()
        flash(f"Student account successfully {'activated' if new_status else 'deactivated'}.", 'success')
    except sqlite3.Error as e:
        if conn:
            conn.rollback()
        flash(f'Database error updating student status: {e}', 'error')
    finally:
        if conn:
            conn.close()
    return redirect(url_for('admin_manage_students'))


@app.route('/admin/students/add', methods=['POST'])
@admin_required
def admin_add_student():
    name = request.form.get('name')
    email = request.form.get('email')
    roll_no = request.form.get('roll_no')
    password = request.form.get('password')
    branch_code = request.form.get('branch_code')
    year = request.form.get('year')

    if not all([name, email, roll_no, password, branch_code, year]):
        flash('All fields are required to add a student.', 'error')
        return redirect(url_for('admin_manage_students'))

    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("""
            INSERT INTO users (name, email, roll_no, password_hash, branch_code, year, role)
            VALUES (?, ?, ?, ?, ?, ?, 'student')
        """, (name, email, roll_no, generate_password_hash(password), branch_code, year))
        conn.commit()
        flash(f'Student {name} added successfully!', 'success')
    except sqlite3.IntegrityError as e:
        if conn:
            conn.rollback()
        if 'users.email' in str(e):
            flash(f'Error: Email "{email}" is already registered.', 'error')
        elif 'users.roll_no' in str(e):
            flash(f'Error: Roll number "{roll_no}" is already registered.', 'error')
        else:
            flash(f'Database integrity error: {e}', 'error')
    except sqlite3.Error as e:
        if conn:
            conn.rollback()
        flash(f'Database error occurred: {e}', 'error')
    finally:
        if conn:
            conn.close()
    return redirect(url_for('admin_manage_students'))


@app.route('/admin/students/edit/<int:student_id>', methods=['GET', 'POST'])
@admin_required
def admin_edit_student(student_id):
    conn = None
    try:
        conn = get_db()
        student = conn.execute("SELECT * FROM users WHERE id = ? AND role = 'student'",
                               (student_id,)).fetchone()
        if not student:
            flash('Student not found.', 'error')
            return redirect(url_for('admin_manage_students'))

        if request.method == 'POST':
            name = request.form.get('name')
            email = request.form.get('email')
            roll_no = request.form.get('roll_no')
            branch_code = request.form.get('branch_code')
            year = request.form.get('year')
            password = request.form.get('password')

            if not all([name, email, roll_no, branch_code, year]):
                flash('All fields except password are required.', 'error')
                return render_template('admin_edit_student.html', student=student, branches=BRANCH_NAMES)

            if password:
                conn.execute("UPDATE users SET name=?, email=?, roll_no=?, branch_code=?, year=?, "
                             "password_hash=? WHERE id = ?",
                             (name, email, roll_no, branch_code, year,
                              generate_password_hash(password), student_id))
            else:
                conn.execute("UPDATE users SET name=?, email=?, roll_no=?, branch_code=?, year=? "
                             "WHERE id = ?", (name, email, roll_no, branch_code, year, student_id))
            conn.commit()
            flash(f'Student {name} updated successfully.', 'success')
            return redirect(url_for('admin_manage_students'))

        return render_template('admin_edit_student.html', student=student, branches=BRANCH_NAMES)

    except sqlite3.IntegrityError:
        if conn:
            conn.rollback()
        flash('Email or roll number already belongs to another student.', 'error')
        return redirect(url_for('admin_manage_students'))
    except sqlite3.Error as e:
        if conn:
            conn.rollback()
        flash(f'Database error: {e}', 'error')
        return redirect(url_for('admin_manage_students'))
    finally:
        if conn:
            conn.close()


@app.route('/admin/students/delete/<int:student_id>', methods=['POST'])
@admin_required
def admin_delete_student(student_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        row = conn.execute("SELECT roll_no FROM users WHERE id = ? AND role = 'student'",
                           (student_id,)).fetchone()
        if not row:
            flash('Student not found.', 'error')
            return redirect(url_for('admin_manage_students'))
        roll_no = row[0]
        conn.execute("DELETE FROM results WHERE student_roll_no = ?", (roll_no,))
        conn.execute("DELETE FROM proctoring_logs WHERE student_roll_no = ?", (roll_no,))
        conn.execute("DELETE FROM users WHERE id = ?", (student_id,))
        conn.commit()
        flash('Student deleted.', 'success')
    except sqlite3.Error as e:
        if conn:
            conn.rollback()
        flash(f'Database error: {e}', 'error')
    finally:
        if conn:
            conn.close()
    return redirect(url_for('admin_manage_students'))


@app.route('/admin/subjects')
@admin_required
def admin_manage_subjects():
    conn = get_db()
    subjects = conn.execute(
        "SELECT id, name, code, branch_code, year, is_active FROM subjects ORDER BY name").fetchall()
    conn.close()
    return render_template('admin_subjects.html', subjects=subjects, branches=BRANCH_NAMES)


@app.route('/admin/subjects/toggle_status/<int:subject_id>', methods=['POST'])
@admin_required
def admin_toggle_subject_status(subject_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        row = conn.execute("SELECT is_active FROM subjects WHERE id = ?", (subject_id,)).fetchone()
        if not row:
            flash('Subject not found.', 'error')
            return redirect(url_for('admin_manage_subjects'))
        new_status = 0 if row[0] == 1 else 1
        conn.execute("UPDATE subjects SET is_active = ? WHERE id = ?", (new_status, subject_id))
        conn.commit()
        flash(f"Subject status successfully {'activated' if new_status else 'deactivated'}.", 'success')
    except sqlite3.Error as e:
        if conn:
            conn.rollback()
        flash(f'Database error: {e}', 'error')
    finally:
        if conn:
            conn.close()
    return redirect(url_for('admin_manage_subjects'))


@app.route('/admin/subjects/add', methods=['POST'])
@admin_required
def admin_add_subject():
    name = request.form.get('name')
    code = request.form.get('code')
    branch_code = request.form.get('branch_code')
    year = request.form.get('year')

    if not all([name, code, branch_code, year]):
        flash('All fields are required to add a subject.', 'error')
        return redirect(url_for('admin_manage_subjects'))

    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("INSERT INTO subjects (name, code, branch_code, year) VALUES (?, ?, ?, ?)",
                     (name, code, branch_code, year))
        conn.commit()
        flash(f'Subject "{name}" added successfully!', 'success')
    except sqlite3.IntegrityError:
        if conn:
            conn.rollback()
        flash(f'Error: Subject code "{code}" already exists.', 'error')
    except sqlite3.Error as e:
        if conn:
            conn.rollback()
        flash(f'Database error occurred: {e}', 'error')
    finally:
        if conn:
            conn.close()
    return redirect(url_for('admin_manage_subjects'))


@app.route('/admin/questions/<int:subject_id>')
@admin_required
def admin_manage_questions(subject_id):
    conn = get_db()
    subject = conn.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,)).fetchone()
    questions = conn.execute("SELECT * FROM questions WHERE subject_id = ?", (subject_id,)).fetchall()
    conn.close()
    if not subject:
        flash("Subject Not Found", "error")
        return redirect(url_for('admin_manage_subjects'))
    return render_template('admin_questions.html', subject=subject, questions=questions)


@app.route('/admin/questions/add/<int:subject_id>', methods=['POST'])
@admin_required
def admin_add_question(subject_id):
    text = request.form.get('text')
    opts = [request.form.get(f'option{i}') for i in range(1, 5)]
    correct = request.form.get('correct_answer')

    if not all([text, *opts, correct]):
        flash('All fields required.', 'error')
        return redirect(url_for('admin_manage_questions', subject_id=subject_id))
    if correct not in opts:
        flash('Correct answer must match one of the options.', 'error')
        return redirect(url_for('admin_manage_questions', subject_id=subject_id))

    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("INSERT INTO questions (text, option1, option2, option3, option4, correct_answer, "
                     "subject_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (text, *opts, correct, subject_id))
        conn.commit()
        flash('Question added.', 'success')
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f'Error adding question: {e}', 'error')
    finally:
        if conn:
            conn.close()
    return redirect(url_for('admin_manage_questions', subject_id=subject_id))


@app.route('/admin/questions/upload/<int:subject_id>', methods=['POST'])
@admin_required
def admin_upload_questions(subject_id):
    """CSV/XLSX with columns: text, option1, option2, option3, option4, correct_answer"""
    file = request.files.get('file')
    if not file or not file.filename:
        flash('No file selected.', 'error')
        return redirect(url_for('admin_manage_questions', subject_id=subject_id))

    conn = None
    try:
        df = pd.read_csv(file) if file.filename.lower().endswith('.csv') else pd.read_excel(file)
        df.columns = [str(c).strip().lower() for c in df.columns]
        required = ['text', 'option1', 'option2', 'option3', 'option4', 'correct_answer']
        missing = [c for c in required if c not in df.columns]
        if missing:
            flash(f"Missing columns: {', '.join(missing)}", 'error')
            return redirect(url_for('admin_manage_questions', subject_id=subject_id))

        rows, skipped = [], 0
        for _, r in df.iterrows():
            vals = [str(r[c]).strip() for c in required]
            if any(v in ('', 'nan') for v in vals) or vals[5] not in vals[1:5]:
                skipped += 1
                continue
            rows.append((*vals, subject_id))

        conn = sqlite3.connect(DB_FILE)
        conn.executemany("INSERT INTO questions (text, option1, option2, option3, option4, correct_answer, "
                         "subject_id) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        flash(f'Uploaded {len(rows)} questions. Skipped {skipped} invalid rows.', 'success')
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f'Upload failed: {e}', 'error')
    finally:
        if conn:
            conn.close()
    return redirect(url_for('admin_manage_questions', subject_id=subject_id))


@app.route('/admin/results', methods=['GET', 'POST'])
@admin_required
def admin_results():
    selected_branch = request.form.get('branch_code', '')
    selected_year = request.form.get('year', '')
    query = """
        SELECT r.id as result_id, r.student_roll_no, u.name as student_name,
               s.name as subject_name, r.score, r.total, r.status, r.timestamp
        FROM results r
        JOIN users u ON r.student_roll_no = u.roll_no
        JOIN subjects s ON r.subject_code = s.code
    """
    params, conditions = [], []
    if selected_branch:
        conditions.append("u.branch_code = ?")
        params.append(selected_branch)
    if selected_year:
        conditions.append("u.year = ?")
        params.append(selected_year)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY r.timestamp DESC"

    conn = get_db()
    results = conn.execute(query, params).fetchall()
    conn.close()
    return render_template('admin_results.html', results=results, branches=BRANCH_NAMES,
                           selected_branch=selected_branch, selected_year=selected_year)


@app.route('/admin/reexam/<int:result_id>', methods=['POST'])
@admin_required
def admin_reexam(result_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.execute("DELETE FROM results WHERE id = ?", (result_id,))
        conn.commit()
        if cur.rowcount > 0:
            flash("Result deleted. The student can now re-take the exam.", "success")
        else:
            flash(f"Warning: Result with ID {result_id} was not found.", "error")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Error deleting result: {e}", "error")
    finally:
        if conn:
            conn.close()
    return redirect(url_for('admin_results'))


# =====================================================================
# API ROUTES
# =====================================================================
@app.route('/api/questions/<subject_code>')
def get_questions(subject_code):
    if 'roll_no' not in session:
        return jsonify({"error": "Not authenticated"}), 401
    conn = None
    try:
        conn = get_db()
        subject_row = conn.execute("SELECT id FROM subjects WHERE code = ?", (subject_code,)).fetchone()
        if not subject_row:
            return jsonify({"error": "Subject not found"}), 404
        questions_from_db = conn.execute(
            "SELECT id, text, option1, option2, option3, option4 FROM questions WHERE subject_id = ?",
            (subject_row['id'],)).fetchall()
    except sqlite3.Error as e:
        print(f"API DB Error: {e}")
        return jsonify({"error": "DB error"}), 500
    finally:
        if conn:
            conn.close()

    if not questions_from_db:
        return jsonify({"error": "No questions found"}), 404
    question_list = [{"id": q['id'], "question": q['text'],
                      "options": [q['option1'], q['option2'], q['option3'], q['option4']]}
                     for q in questions_from_db]
    random.shuffle(question_list)
    return jsonify(question_list)


@app.route('/submit', methods=['POST'])
def submit_exam():
    if 'roll_no' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    data = request.get_json(silent=True) or {}
    subject_code = data.get('subject')
    answers = data.get('answers', {})
    reason = data.get('reason', 'Completed normally')
    student_roll_no = str(session['roll_no'])
    if not subject_code or not isinstance(answers, dict):
        return jsonify({'error': 'Invalid data'}), 400

    conn = get_db()
    try:
        subject_row = conn.execute("SELECT id FROM subjects WHERE code = ?", (subject_code,)).fetchone()
        if not subject_row:
            raise ValueError("Subject not found")
        correct_db = {str(q['id']): q['correct_answer'] for q in conn.execute(
            "SELECT id, correct_answer FROM questions WHERE subject_id = ?", (subject_row['id'],))}
        if not correct_db:
            raise ValueError("No questions found")

        score = sum(1 for q_id, ans in answers.items() if correct_db.get(str(q_id)) == ans)
        total_questions = len(correct_db)
        status = "Completed" if ("completed" in reason.lower() or "time ran out" in reason.lower()) \
            else "Terminated"
        redirect_url = url_for('exam_terminated') if status == "Terminated" else url_for('submission_success')

        if conn.execute("SELECT id FROM results WHERE student_roll_no = ? AND subject_code = ?",
                        (student_roll_no, subject_code)).fetchone():
            return jsonify({'status': 'already_submitted', 'redirect_url': redirect_url})

        conn.execute("INSERT INTO results (student_roll_no, subject_code, score, total, status, reason) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (student_roll_no, subject_code, score, total_questions, status, reason))
        conn.commit()
        print(f"SERVER: Result saved for {student_roll_no}. Score: {score}/{total_questions}. {status}")

        if student_roll_no in ACTIVE_EXAMS:
            del ACTIVE_EXAMS[student_roll_no]
            socketio.emit('student_left_exam', {'roll_no': student_roll_no, 'reason': status},
                          room='admin_room')
        return jsonify({'status': 'success', 'redirect_url': redirect_url})
    except ValueError as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 400
    except sqlite3.Error as e:
        conn.rollback()
        print(f"Submit DB Error: {e}")
        return jsonify({'error': 'DB error'}), 500
    finally:
        conn.close()


# =====================================================================
# SOCKET.IO EVENTS
# =====================================================================
@socketio.on('connect')
def handle_connect():
    print(f"SERVER: Client connected: {request.sid}")


@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    roll_no_str = connected_students.pop(sid, None)
    if roll_no_str:
        print(f"SERVER: Student {roll_no_str} disconnected: {sid}")
        if roll_no_str in ACTIVE_EXAMS:
            log_proctoring_event(roll_no_str, 'SOCKET_DISCONNECT', 'Student connection lost.')
            emit('student_left_exam', {'roll_no': roll_no_str, 'reason': 'Disconnected'}, room='admin_room')


@socketio.on('admin_join')
def handle_admin_join():
    sid = request.sid
    if not session.get('admin'):
        print(f"SERVER: Unauthorized admin_join attempt by {sid}")
        return
    join_room('admin_room')
    emit('active_students_list', ACTIVE_EXAMS, room=sid)


@socketio.on('student_join')
def handle_student_join(data):
    sid = request.sid
    roll_no = data.get('roll_no')
    roll_no_str = str(roll_no) if roll_no else None
    if roll_no_str and roll_no_str in ACTIVE_EXAMS:
        connected_students[sid] = roll_no_str
        join_room(roll_no_str)
        emit('student_started_exam', {'roll_no': roll_no_str, 'info': ACTIVE_EXAMS.get(roll_no_str)},
             room='admin_room')
    else:
        print(f"SERVER: Student join rejected. Roll: {roll_no_str}, Active: {list(ACTIVE_EXAMS.keys())}")


@socketio.on('student_leave')
def handle_student_leave(data):
    sid = request.sid
    roll_no = data.get('roll_no')
    roll_no_str = str(roll_no) if roll_no else None
    if connected_students.get(sid) == roll_no_str:
        del connected_students[sid]


@socketio.on('video_frame_from_student')
def handle_video_frame(data):
    sid = request.sid
    roll_no = data.get('roll_no')
    frame_b64 = data.get('frame')
    roll_no_str = str(roll_no) if roll_no else None
    if not roll_no_str or not frame_b64 or connected_students.get(sid) != roll_no_str:
        return
    emit('video_frame', {'roll_no': roll_no_str, 'frame': frame_b64}, room='admin_room')
    if not ai_model_loaded:
        return
    try:
        img_data = base64.b64decode(frame_b64.split(',')[1])
        img = cv2.imdecode(np.frombuffer(img_data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return
        blob = cv2.dnn.blobFromImage(img, 0.00392, (320, 320), (0, 0, 0), True, crop=False)
        net.setInput(blob)
        outs = net.forward(output_layers)

        detected = []
        for out in outs:
            for detection in out:
                scores = detection[5:]
                class_id = int(np.argmax(scores))
                if scores[class_id] > 0.5 and classes:
                    detected.append(str(classes[class_id]))

        person_count = detected.count('person')
        if person_count > 1:
            msg = f'Multiple People ({person_count})'
            log_proctoring_event(roll_no_str, 'AI_ALERT_PERSON', msg)
            emit('proctoring_alert', {'roll_no': roll_no_str, 'alert': msg}, room='admin_room')
            emit('receive_warning', {'message': 'Multiple people detected. Please ensure you are alone.'},
                 room=roll_no_str)
        if 'cell phone' in detected:
            msg = 'Cell Phone Detected'
            log_proctoring_event(roll_no_str, 'AI_ALERT_PHONE', msg)
            emit('proctoring_alert', {'roll_no': roll_no_str, 'alert': msg}, room='admin_room')
            emit('receive_warning', {'message': 'Mobile phone detected. Please put it away immediately.'},
                 room=roll_no_str)
    except Exception as e:
        print(f"AI Error ({roll_no_str}): {e}")


@socketio.on('proctoring_violation')
def handle_proctoring_violation(data):
    sid = request.sid
    roll_no = data.get('roll_no')
    violation_type = data.get('type')
    message = data.get('message')
    roll_no_str = str(roll_no) if roll_no else None
    if roll_no_str and violation_type and connected_students.get(sid) == roll_no_str:
        log_proctoring_event(roll_no_str, violation_type, message)
        emit('proctoring_alert', {'roll_no': roll_no_str, 'alert': f"{violation_type}: {message}"},
             room='admin_room')
    else:
        print(f"SERVER: Invalid violation data/SID mismatch: {data}")


@socketio.on('audio_alert')
def handle_audio_alert(data):
    sid = request.sid
    roll_no = data.get('roll_no')
    roll_no_str = str(roll_no) if roll_no else None
    if roll_no_str and connected_students.get(sid) == roll_no_str:
        message = "Suspicious noise."
        log_proctoring_event(roll_no_str, 'AUDIO_ALERT', message)
        emit('proctoring_alert', {'roll_no': roll_no_str, 'alert': message}, room='admin_room')
        emit('receive_warning', {'message': 'Suspicious noise detected. Please remain silent.'},
             room=roll_no_str)
    else:
        print(f"SERVER: Invalid audio alert data/SID mismatch: {data}")


@socketio.on('send_warning')
def handle_send_warning(data):
    if not session.get('admin'):
        return
    roll_no_str = str(data.get('student_roll_no')) if data.get('student_roll_no') else None
    message = data.get('message')
    if roll_no_str and message:
        emit('receive_warning', {'message': message}, room=roll_no_str)
        log_proctoring_event(roll_no_str, 'ADMIN_WARNING', message)


@socketio.on('terminate_exam')
def handle_terminate_exam(data):
    if not session.get('admin'):
        return
    roll_no_str = str(data.get('student_roll_no')) if data.get('student_roll_no') else None
    if roll_no_str:
        reason = "Terminated by administrator."
        emit('exam_terminated', {'reason': reason}, room=roll_no_str)
        log_proctoring_event(roll_no_str, 'ADMIN_TERMINATE', reason)


# =====================================================================
# MAIN
# =====================================================================
if __name__ == '__main__':
    init_db()
    print("Starting Flask-SocketIO server on http://127.0.0.1:5000")
    print(f"Admin face verification: {'ON' if FACE_VERIFICATION_ENABLED else 'OFF'}")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, use_reloader=False)