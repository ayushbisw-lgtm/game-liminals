from flask import Flask, send_from_directory, request, jsonify
from flask_cors import CORS
from werkzeug.utils import secure_filename
import os
import json
import datetime
import sqlite3
import threading
from github_sync import GitHubSync

# Create Flask app
app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Initialize GitHub Sync
gh_sync = GitHubSync()

def trigger_github_sync(file_path):
    """Triggers GitHub sync in a background thread."""
    if gh_sync.token:
        thread = threading.Thread(target=gh_sync.sync_file, args=(file_path,))
        thread.daemon = True
        thread.start()

def trigger_multiple_sync(file_paths):
    """Triggers GitHub sync for multiple files."""
    for path in file_paths:
        trigger_github_sync(path)

# In-memory storage for active members {member_id: timestamp}
active_members = {}

# Define absolute paths for data files
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_FILE = os.path.join(BASE_DIR, 'database.db')
PROJECTS_FILE = os.path.join(BASE_DIR, 'projects.json')
TEAM_FILE = os.path.join(BASE_DIR, 'team.json')
MESSAGES_FILE = os.path.join(BASE_DIR, 'messages.json')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Projects Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY,
            name TEXT,
            assignee TEXT,
            deadline TEXT,
            priority TEXT,
            description TEXT,
            completed INTEGER,
            created_at TEXT,
            status TEXT,
            submission_link TEXT,
            submission_notes TEXT,
            submission_file TEXT,
            submitted_at TEXT
        )
    ''')
    
    # Check for missing columns (migration for existing db)
    existing_cols = [row[1] for row in cursor.execute("PRAGMA table_info(projects)")]
    new_cols = {
        'status': 'TEXT',
        'submission_link': 'TEXT',
        'submission_notes': 'TEXT',
        'submission_file': 'TEXT',
        'submitted_at': 'TEXT'
    }
    
    for col, dtype in new_cols.items():
        if col not in existing_cols:
            print(f"Migrating DB: Adding column {col}")
            cursor.execute(f'ALTER TABLE projects ADD COLUMN {col} {dtype}')

    # Team Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS team (
            id INTEGER PRIMARY KEY,
            name TEXT,
            email TEXT,
            phone TEXT,
            password TEXT,
            approved INTEGER DEFAULT 1
        )
    ''')
    
    # Check if approved column exists for migration
    cursor.execute("PRAGMA table_info(team)")
    cols = [c[1] for c in cursor.fetchall()]
    if 'approved' not in cols:
        print("Migrating DB: Adding approved column to team table")
        cursor.execute('ALTER TABLE team ADD COLUMN approved INTEGER DEFAULT 1')
    
    # Messages Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT,
            timestamp TEXT
        )
    ''')
    
    conn.commit()
    conn.close()

# Initialize DB on start
init_db()

# --- HEARTBEAT API ---
@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    try:
        data = request.json
        member_id = str(data.get('memberId'))
        if member_id:
            active_members[member_id] = datetime.datetime.now()
            return jsonify({'status': 'success'})
        return jsonify({'status': 'error', 'message': 'No memberId provided'}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

def migrate_data():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Migrate Projects
    cursor.execute('SELECT count(*) FROM projects')
    if cursor.fetchone()[0] == 0 and os.path.exists(PROJECTS_FILE):
        try:
            with open(PROJECTS_FILE, 'r') as f:
                projects = json.load(f)
                for p in projects:
                    cursor.execute('''
                        INSERT INTO projects (id, name, assignee, deadline, priority, description, completed, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        p.get('id'),
                        p.get('name'),
                        p.get('assignee'),
                        p.get('deadline'),
                        p.get('priority'),
                        p.get('description', ''),
                        1 if p.get('completed') else 0,
                        p.get('createdAt')
                    ))
            conn.commit()
            print("Migrated projects.json to SQLite")
        except Exception as e:
            print(f"Project migration error: {e}")

    # Migrate Team
    cursor.execute('SELECT count(*) FROM team')
    if cursor.fetchone()[0] == 0 and os.path.exists(TEAM_FILE):
        try:
            with open(TEAM_FILE, 'r') as f:
                team = json.load(f)
                for t in team:
                    cursor.execute('''
                        INSERT INTO team (id, name, email, phone, password, approved)
                        VALUES (?, ?, ?, ?, ?, 1)
                    ''', (
                        t.get('id'),
                        t.get('name'),
                        t.get('email'),
                        t.get('phone'),
                        t.get('password', 'User123')
                    ))
            conn.commit()
            print("Migrated team.json to SQLite")
        except Exception as e:
            print(f"Team migration error: {e}")

    # Migrate Messages
    cursor.execute('SELECT count(*) FROM messages')
    if cursor.fetchone()[0] == 0 and os.path.exists(MESSAGES_FILE):
        try:
            with open(MESSAGES_FILE, 'r') as f:
                messages = json.load(f)
                for m in messages:
                    cursor.execute('INSERT INTO messages (data, timestamp) VALUES (?, ?)', 
                                 (json.dumps(m), datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            print("Migrated messages.json to SQLite")
        except Exception as e:
            print(f"Message migration error: {e}")
            
    conn.close()

# Run migration
migrate_data()

@app.route('/api/login', methods=['POST'])
def login():
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'success': False, 'message': 'Missing request body'}), 400
            
        member_id = data.get('memberId')
        password = data.get('password')
        
        if not member_id or not password:
            return jsonify({'success': False, 'message': 'Missing credentials'}), 400
            
        conn = get_db_connection()
        # Find member by ID and Password (must be approved)
        member = conn.execute('SELECT * FROM team WHERE CAST(id AS TEXT) = ? AND password = ?', (str(member_id), password)).fetchone()
        
        if member:
            if member['approved'] == 1:
                conn.close()
                return jsonify({'success': True, 'name': member['name']})
            else:
                conn.close()
                return jsonify({'success': False, 'message': 'Registration pending approval'})
        else:
            conn.close()
            return jsonify({'success': False, 'message': 'Invalid credentials'})
            
    except Exception as e:
        print(f"Login error: {e}")
        return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500

@app.route('/api/register', methods=['POST'])
def register():
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'success': False, 'message': 'Missing request body'}), 400
            
        name = data.get('name')
        email = data.get('email')
        phone = data.get('phone')
        
        if not all([name, email, phone]):
            return jsonify({'success': False, 'message': 'All fields are required'}), 400
            
        # Generate a default password based on the first name
        first_name = name.strip().split()[0].capitalize()
        password = f"{first_name}123"
            
        conn = get_db_connection()
        
        # Check if email or phone already exists
        existing = conn.execute('SELECT * FROM team WHERE email = ? OR phone = ?', (email, phone)).fetchone()
        if existing:
            conn.close()
            return jsonify({'success': False, 'message': 'Email or Phone already registered'})
            
        # Get next available ID
        max_id_row = conn.execute('SELECT MAX(id) as max_id FROM team').fetchone()
        next_id = (max_id_row['max_id'] or 0) + 1
        
        # Insert new member (Pending Approval)
        conn.execute('''
            INSERT INTO team (id, name, email, phone, password, approved)
            VALUES (?, ?, ?, ?, ?, 0)
        ''', (next_id, name, email, phone, password))
        conn.commit()
        
        # Trigger GitHub Sync
        trigger_github_sync(DATABASE_FILE)
        
        # We don't update JSON here because it's only for approved members
        # The admin will approve it and then it will be added to JSON
            
        conn.close()
        
        return jsonify({
            'success': True, 
            'message': 'Registration submitted! Please wait for admin approval before logging in.', 
            'memberId': next_id,
            'pending': True
        })
        
    except Exception as e:
        print(f"Registration error: {e}")
        return jsonify({'success': False, 'message': f'Server error: {str(e)}'}), 500

# --- MESSAGES API ---
@app.route('/api/messages', methods=['GET'])
def get_messages():
    conn = get_db_connection()
    messages = conn.execute('SELECT * FROM messages').fetchall()
    conn.close()
    
    result = []
    for m in messages:
        try:
            result.append(json.loads(m['data']))
        except:
            pass
    return jsonify(result)

@app.route('/api/messages', methods=['POST'])
def save_message():
    try:
        new_message = request.json
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        conn = get_db_connection()
        conn.execute('INSERT INTO messages (data, timestamp) VALUES (?, ?)', 
                     (json.dumps(new_message), timestamp))
        conn.commit()
        conn.close()

        # Save to JSON file as well (Backup/User Visibility)
        try:
            # Get all messages to save to JSON
            conn = get_db_connection()
            messages = conn.execute('SELECT * FROM messages').fetchall()
            conn.close()
            msg_list = [json.loads(m['data']) for m in messages]
            with open(MESSAGES_FILE, 'w') as f:
                json.dump(msg_list, f, indent=4)
            trigger_multiple_sync([MESSAGES_FILE, DATABASE_FILE])
        except Exception as e:
            print(f"Warning: Failed to save messages.json: {e}")

        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/messages', methods=['DELETE'])
def delete_messages():
    try:
        conn = get_db_connection()
        conn.execute('DELETE FROM messages')
        conn.commit()
        conn.close()
        trigger_github_sync(DATABASE_FILE)
        return jsonify({'status': 'cleared'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# --- PROJECTS API ---
@app.route('/api/projects', methods=['GET'])
def get_projects():
    print("API HIT: get_projects")
    conn = get_db_connection()
    projects = conn.execute('SELECT * FROM projects').fetchall()
    conn.close()
    
    result = []
    for p in projects:
        result.append({
            'id': p['id'],
            'name': p['name'],
            'assignee': p['assignee'],
            'deadline': p['deadline'],
            'priority': p['priority'],
            'description': p['description'],
            'completed': bool(p['completed']),
            'createdAt': p['created_at'],
            'status': p['status'],
            'submissionLink': p['submission_link'],
            'submissionNotes': p['submission_notes'],
            'submissionFile': p['submission_file'],
            'submittedAt': p['submitted_at']
        })
    return jsonify(result)

@app.route('/api/projects', methods=['POST'])
def save_projects():
    print("API HIT: save_projects")
    try:
        projects_data = request.json
        conn = get_db_connection()
        # Transaction to replace all
        conn.execute('DELETE FROM projects')
        for p in projects_data:
             conn.execute('''
                INSERT INTO projects (
                    id, name, assignee, deadline, priority, description, completed, created_at,
                    status, submission_link, submission_notes, submission_file, submitted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                p.get('id'),
                p.get('name'),
                p.get('assignee'),
                p.get('deadline'),
                p.get('priority'),
                p.get('description', ''),
                1 if p.get('completed') else 0,
                p.get('createdAt'),
                p.get('status'),
                p.get('submissionLink'),
                p.get('submissionNotes'),
                p.get('submissionFile'),
                p.get('submittedAt')
            ))
        conn.commit()
        conn.close()

        # Save to JSON file as well (Backup/User Visibility)
        try:
            with open(PROJECTS_FILE, 'w') as f:
                json.dump(projects_data, f, indent=4)
            trigger_multiple_sync([PROJECTS_FILE, DATABASE_FILE])
        except Exception as e:
            print(f"Warning: Failed to save projects.json: {e}")

        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/submit-project', methods=['POST'])
def submit_project():
    print("API HIT: submit_project")
    try:
        project_id = request.form.get('projectId')
        link = request.form.get('link')
        notes = request.form.get('notes')
        file = request.files.get('file')
        
        filename = None
        sync_files = [DATABASE_FILE]
        if file and file.filename:
            filename = secure_filename(f"{project_id}_{file.filename}")
            file_path = os.path.join(UPLOAD_FOLDER, filename)
            file.save(file_path)
            sync_files.append(file_path)
        
        conn = get_db_connection()
        conn.execute('''
            UPDATE projects 
            SET status = 'submitted', 
                submission_link = ?, 
                submission_notes = ?, 
                submission_file = ?, 
                submitted_at = ?
            WHERE id = ?
        ''', (link, notes, filename, datetime.datetime.now().isoformat(), project_id))
        conn.commit()
        conn.close()
        
        trigger_multiple_sync(sync_files)
        
        return jsonify({'status': 'success'})
    except Exception as e:
        print(f"Submit Error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# --- TEAM API ---
@app.route('/api/team', methods=['GET'])
def get_team():
    print("API HIT: get_team")
    conn = get_db_connection()
    # Only return approved members for public list
    team = conn.execute('SELECT * FROM team WHERE approved = 1').fetchall()
    conn.close()
    
    result = []
    now = datetime.datetime.now()
    for t in team:
        m_id = str(t['id'])
        is_online = False
        if m_id in active_members:
            last_seen = active_members[m_id]
            # Consider online if seen in last 30 seconds
            if (now - last_seen).total_seconds() < 30:
                is_online = True
            else:
                # Cleanup old entries
                del active_members[m_id]

        result.append({
            'id': t['id'],
            'name': t['name'],
            'email': t['email'],
            'phone': t['phone'],
            'password': t['password'],
            'online': is_online
        })
    return jsonify(result)

@app.route('/api/team', methods=['POST'])
def save_team():
    print("API HIT: save_team")
    try:
        team_data = request.json
        conn = get_db_connection()
        
        # 1. Fetch existing passwords to preserve them
        existing_passwords = {}
        rows = conn.execute('SELECT id, password FROM team').fetchall()
        for r in rows:
            existing_passwords[r['id']] = r['password']
            
        # 2. Delete only approved members to preserve pending registrations
        conn.execute('DELETE FROM team WHERE approved = 1')
        
        for t in team_data:
            # Preserve password or set default
            pwd = existing_passwords.get(t.get('id'))
            if not pwd:
                # New user or lost password? Generate default based on name
                first_name = t.get('name', 'User').split()[0].capitalize()
                pwd = f"{first_name}123"
            
            conn.execute('''
                INSERT INTO team (id, name, email, phone, password, approved)
                VALUES (?, ?, ?, ?, ?, 1)
            ''', (
                t.get('id'),
                t.get('name'),
                t.get('email'),
                t.get('phone'),
                pwd,
            ))
        conn.commit()
        conn.close()

        # Save to JSON file as well (Backup/User Visibility)
        try:
            with open(TEAM_FILE, 'w') as f:
                json.dump(team_data, f, indent=4)
            trigger_multiple_sync([TEAM_FILE, DATABASE_FILE])
        except Exception as e:
            print(f"Warning: Failed to save team.json: {e}")

        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# --- ADMIN APPROVAL API ---
@app.route('/api/admin/pending', methods=['GET'])
def get_pending_members():
    try:
        conn = get_db_connection()
        pending = conn.execute('SELECT * FROM team WHERE approved = 0').fetchall()
        conn.close()
        
        result = []
        for p in pending:
            result.append({
                'id': p['id'],
                'name': p['name'],
                'email': p['email'],
                'phone': p['phone']
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/admin/approve', methods=['POST'])
def approve_member():
    try:
        data = request.json
        member_id = data.get('id')
        if not member_id:
            return jsonify({'success': False, 'message': 'Missing member ID'}), 400
            
        conn = get_db_connection()
        conn.execute('UPDATE team SET approved = 1 WHERE id = ?', (member_id,))
        conn.commit()
        
        # Get member details after approval to return to admin
        member = conn.execute('SELECT * FROM team WHERE id = ?', (member_id,)).fetchone()
        
        # Sync to team.json after approval
        team_members = conn.execute('SELECT * FROM team WHERE approved = 1').fetchall()
        team_list = []
        for t in team_members:
            team_list.append({
                'id': t['id'],
                'name': t['name'],
                'email': t['email'],
                'phone': t['phone'],
                'password': t['password']
            })
        
        try:
            with open(TEAM_FILE, 'w') as f:
                json.dump(team_list, f, indent=4)
            trigger_multiple_sync([TEAM_FILE, DATABASE_FILE])
        except Exception as e:
            print(f"Warning: Failed to sync team.json: {e}")
            
        conn.close()
        return jsonify({
            'success': True, 
            'message': 'Member approved successfully',
            'member': {
                'id': member['id'],
                'name': member['name'],
                'email': member['email'],
                'phone': member['phone'],
                'password': member['password']
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/admin/reject', methods=['POST'])
def reject_member():
    try:
        data = request.json
        member_id = data.get('id')
        if not member_id:
            return jsonify({'success': False, 'message': 'Missing member ID'}), 400
            
        conn = get_db_connection()
        conn.execute('DELETE FROM team WHERE id = ? AND approved = 0', (member_id,))
        conn.commit()
        conn.close()
        trigger_github_sync(DATABASE_FILE)
        return jsonify({'success': True, 'message': 'Registration rejected and deleted'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

# --- ROUTES ---
@app.route('/')
def home():
    return send_from_directory('.', 'index.html')

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({'status': 'error', 'message': 'API endpoint not found'}), 404
    return send_from_directory('.', 'index.html')

@app.errorhandler(500)
def server_error(e):
    if request.path.startswith('/api/'):
        return jsonify({'status': 'error', 'message': 'Internal server error'}), 500
    return "Internal Server Error", 500

# Serve uploaded files
@app.route('/uploads/<path:filename>')
def serve_uploads(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

# Serve static files (css, js, images) - MUST BE LAST
@app.route('/<path:path>')
def serve_static_files(path):
    # Ensure path doesn't start with a slash for os.path.join
    safe_path = path.lstrip('/')
    print(f"STATIC REQUEST: {safe_path}")
    
    # Special handling for common static types to ensure correct MIME
    if safe_path.endswith('.mp4'):
        full_path = os.path.join(BASE_DIR, safe_path)
        if os.path.exists(full_path):
            print(f"SERVING MP4: {full_path}")
            return send_from_directory('.', safe_path, mimetype='video/mp4', conditional=True)
        else:
            print(f"MP4 NOT FOUND: {full_path}")
            return send_from_directory('.', 'index.html')
    
    if safe_path.endswith('.js'):
        return send_from_directory('.', safe_path, mimetype='application/javascript')
    if safe_path.endswith('.css'):
        return send_from_directory('.', safe_path, mimetype='text/css')
    
    return send_from_directory('.', safe_path)


if __name__ == '__main__':
    # Get port from environment variable or default to 8000
    port = int(os.environ.get('PORT', 8000))
    print(f"Starting server on port {port}...")
    app.run(host='0.0.0.0', port=port)
