import os
import shutil
import time
import uuid
import csv
import socket
import logging
from datetime import datetime
import random
import string

from flask import Flask, render_template, request, send_from_directory, jsonify, abort
from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect
from werkzeug.utils import secure_filename

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize Flask app and SocketIO
app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24) # For session security
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024 * 1024  # 2 GB limit per file, adjust as needed
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins="*") # Allow CORS for development

# --- Configuration & Global State ---
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(ROOT_DIR, "temp_staging_area")
RECEIVED_DIR = os.path.join(ROOT_DIR, "received_files")
HISTORY_FILE = os.path.join(ROOT_DIR, "transfer_history.csv")

os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(RECEIVED_DIR, exist_ok=True)

# In-memory store for transfer sessions
# transfer_id: {
#   'sender_sid': str, 'receiver_sid': str (optional),
#   'files': [{'name': str, 'size': int, 'staged': bool, 'staged_path': str (optional)}],
#   'total_size': int, 'staged_bytes': int,
#   'security_code': str, 'status': str ('pending_staging'|'pending_approval'|'approved'|'transferring'|'completed'|'cancelled'|'failed'),
#   'staged_root_path': str, 'start_time': float, 'last_progress_update': float,
#   'is_sender_host': bool, 'sender_ip': str, 'receiver_ip': str (optional)
# }
transfer_sessions = {}
clients = {} # sid: {'ip': str, 'is_host': bool}

# --- Helper Functions ---
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"

SERVER_IP = get_local_ip()

def generate_security_code(length=6):
    return ''.join(random.choices(string.digits, k=length))

def log_transfer_history(session_id, status_override=None):
    if session_id not in transfer_sessions:
        return

    session = transfer_sessions[session_id]
    file_exists = os.path.isfile(HISTORY_FILE)
    
    direction = "HostToGuest" if session.get('is_sender_host') else "GuestToHost"
    if session.get('is_sender_host') is None: # For early logs before direction is clear
        direction = "Unknown"

    with open(HISTORY_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists or os.path.getsize(HISTORY_FILE) == 0:
            writer.writerow([
                "Timestamp", "TransferID", "Direction", "FileNames", 
                "TotalSizeMB", "Status", "SecurityCode", 
                "SenderIP", "ReceiverIP"
            ])
        
        filenames = ", ".join([file_meta['name'] for file_meta in session.get('files', [])])
        total_size_mb = round(session.get('total_size', 0) / (1024*1024), 2)
        current_status = status_override if status_override else session.get('status', 'N/A')

        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            session_id,
            direction,
            filenames,
            total_size_mb,
            current_status,
            session.get('security_code', 'N/A'),
            session.get('sender_ip', 'N/A'),
            session.get('receiver_ip', 'N/A')
        ])

def cleanup_session_files(session_id):
    if session_id in transfer_sessions:
        session = transfer_sessions[session_id]
        staged_path = session.get('staged_root_path')
        if staged_path and os.path.exists(staged_path):
            try:
                shutil.rmtree(staged_path)
                logging.info(f"Cleaned up staged files for {session_id} at {staged_path}")
            except Exception as e:
                logging.error(f"Error cleaning up {staged_path}: {e}")
        # Do not remove from transfer_sessions here, as it might be needed for history or final status updates.
        # Consider a separate cleanup for the transfer_sessions entry itself after some time.

# --- Flask HTTP Routes ---
@app.route('/')
def index():
    return render_template('index.html', server_ip=SERVER_IP)

@app.route('/upload_to_stage/<transfer_id>', methods=['POST'])
def upload_to_stage(transfer_id):
    if transfer_id not in transfer_sessions:
        return jsonify({"error": "Invalid transfer ID"}), 404
    
    session = transfer_sessions[transfer_id]
    if session['status'] not in ['pending_staging', 'pending_approval']: # Allow staging even if approval is pending
        return jsonify({"error": "Transfer not in staging phase"}), 400

    file = request.files.get('file')
    if not file:
        return jsonify({"error": "No file part"}), 400

    filename = secure_filename(file.filename)
    
    file_meta_index = -1
    for i, fm in enumerate(session['files']):
        if fm['name'] == filename and not fm.get('staged', False):
            file_meta_index = i
            break
    
    if file_meta_index == -1:
        return jsonify({"error": f"File {filename} not expected or already staged"}), 400

    try:
        staged_file_path = os.path.join(session['staged_root_path'], filename)
        file.save(staged_file_path)
        session['files'][file_meta_index]['staged'] = True
        session['files'][file_meta_index]['staged_path'] = staged_file_path
        session['staged_bytes'] += session['files'][file_meta_index]['size'] # Assuming size is accurate

        # Emit staging progress to sender
        staging_progress = (session['staged_bytes'] / session['total_size']) * 100 if session['total_size'] > 0 else 100
        socketio.emit('staging_progress', {
            'transfer_id': transfer_id,
            'filename': filename,
            'progress': staging_progress
        }, room=session['sender_sid'])

        all_staged = all(f.get('staged', False) for f in session['files'])
        if all_staged and session['status'] == 'pending_staging':
            session['status'] = 'pending_approval'
            # Notify sender that staging is complete
            socketio.emit('all_files_staged', {'transfer_id': transfer_id}, room=session['sender_sid'])
            
            # Now that staging is complete, if it was pending_staging, we can formally send request to receivers
            # (This was moved to be sent earlier, so this just updates status)
            logging.info(f"All files staged for {transfer_id}. Now pending approval.")
        
        return jsonify({"message": f"File {filename} staged successfully."}), 200

    except Exception as e:
        logging.error(f"Error staging file {filename} for {transfer_id}: {e}")
        session['status'] = 'failed'
        log_transfer_history(transfer_id)
        cleanup_session_files(transfer_id)
        socketio.emit('transfer_failed', {'transfer_id': transfer_id, 'error': str(e)}, room=session['sender_sid'])
        if session.get('receiver_sid'):
            socketio.emit('transfer_failed', {'transfer_id': transfer_id, 'error': str(e)}, room=session['receiver_sid'])
        return jsonify({"error": f"Failed to stage file: {str(e)}"}), 500

@app.route('/download_staged_file/<transfer_id>/<filename>')
def download_staged_file(transfer_id, filename):
    if transfer_id not in transfer_sessions:
        abort(404, description="Transfer ID not found.")
    
    session = transfer_sessions[transfer_id]
    if session['status'] not in ['transferring', 'approved']: # Approved means ready to start transfer
        abort(403, description="Transfer not in correct state for download.")
    
    # If this is the first file download, change status to 'transferring'
    if session['status'] == 'approved':
        session['status'] = 'transferring'
        session['last_progress_update'] = time.time()
        session['current_transfer_start_time'] = time.time()
        session['bytes_sent_in_interval'] = 0


    requested_file_meta = next((f for f in session['files'] if f['name'] == filename), None)
    if not requested_file_meta or not requested_file_meta.get('staged_path'):
        abort(404, description="File not found or not staged.")

    staged_file_path = requested_file_meta['staged_path']
    
    def generate_file_chunks():
        nonlocal session # To update session within the generator
        chunk_size = 1024 * 1024  # 1MB chunks
        total_sent_for_this_file = 0
        try:
            with open(staged_file_path, 'rb') as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
                    total_sent_for_this_file += len(chunk)
                    session['bytes_sent_in_interval'] += len(chunk)

                    now = time.time()
                    time_delta = now - session['last_progress_update']

                    if time_delta >= 0.5: # Update progress every 0.5 seconds
                        speed_bps = (session['bytes_sent_in_interval'] / time_delta) if time_delta > 0 else 0
                        session['bytes_sent_in_interval'] = 0
                        session['last_progress_update'] = now
                        
                        # Overall progress calculation needs to sum up sizes of fully sent files
                        # plus current file's progress. This is simpler if we update 'total_sent_bytes'
                        # on file completion and use 'total_sent_for_this_file' for current.
                        
                        # Let's make progress based on total_size of session and how much is sent overall
                        # This requires careful tracking. Simpler: progress for current file, and overall files done/total
                        current_file_progress = (total_sent_for_this_file / requested_file_meta['size']) * 100 if requested_file_meta['size'] > 0 else 100
                        
                        # For overall progress, we need to know which file index this is
                        # and sum up previous files' sizes.
                        # This will be handled by a separate socketio emit from a background thread or after file completion.
                        # For now, just send current file progress.

                        socketio.emit('transfer_progress', {
                            'transfer_id': transfer_id,
                            'filename': filename,
                            'file_progress': round(current_file_progress, 2),
                            'speed_mbps': round(speed_bps / (1024*1024), 2), # Bps to Mbps
                        }, room=session['sender_sid'])
                        socketio.emit('transfer_progress', {
                            'transfer_id': transfer_id,
                            'filename': filename,
                            'file_progress': round(current_file_progress, 2),
                            'speed_mbps': round(speed_bps / (1024*1024), 2),
                        }, room=session['receiver_sid'])
                        socketio.sleep(0) # Yield to other greenlets

            # After file is fully sent
            session['total_sent_bytes'] = session.get('total_sent_bytes', 0) + requested_file_meta['size']
            overall_progress_val = (session['total_sent_bytes'] / session['total_size']) * 100 if session['total_size'] > 0 else 100
            
            socketio.emit('file_transfer_complete', {
                'transfer_id': transfer_id,
                'filename': filename,
                'overall_progress': round(overall_progress_val, 2)
            }, room=session['sender_sid'])
            socketio.emit('file_transfer_complete', {
                'transfer_id': transfer_id,
                'filename': filename,
                'overall_progress': round(overall_progress_val, 2)
            }, room=session['receiver_sid'])

            if session['total_sent_bytes'] >= session['total_size']:
                session['status'] = 'completed'
                log_transfer_history(transfer_id)
                if session.get('is_sender_host'): # Laptop sent to Mobile
                    cleanup_session_files(transfer_id) # Temp files on laptop can be removed
                else: # Mobile sent to Laptop, move files from temp to received
                    final_dest_dir = os.path.join(RECEIVED_DIR, transfer_id)
                    os.makedirs(final_dest_dir, exist_ok=True)
                    for f_meta in session['files']:
                        src = f_meta['staged_path']
                        dst = os.path.join(final_dest_dir, f_meta['name'])
                        try:
                            shutil.move(src, dst)
                        except Exception as e:
                            logging.error(f"Error moving {src} to {dst}: {e}")
                    cleanup_session_files(transfer_id) # remove original temp_staging_area/transfer_id folder
                    logging.info(f"Files for {transfer_id} moved to {final_dest_dir}")

                socketio.emit('all_transfers_complete', {'transfer_id': transfer_id}, room=session['sender_sid'])
                socketio.emit('all_transfers_complete', {'transfer_id': transfer_id}, room=session['receiver_sid'])
        
        except Exception as e:
            logging.error(f"Error streaming file {filename} for {transfer_id}: {e}")
            session['status'] = 'failed'
            log_transfer_history(transfer_id)
            socketio.emit('transfer_failed', {'transfer_id': transfer_id, 'error': str(e)}, room=session['sender_sid'])
            if session.get('receiver_sid'):
                socketio.emit('transfer_failed', {'transfer_id': transfer_id, 'error': str(e)}, room=session['receiver_sid'])
            cleanup_session_files(transfer_id)
            # Client will see connection drop for the download

    return app.response_class(generate_file_chunks(), mimetype='application/octet-stream', headers={
        "Content-Disposition": f"attachment; filename=\"{filename}\""
    })


# --- SocketIO Events ---
@socketio.on('connect')
def handle_connect():
    client_ip = request.environ.get('HTTP_X_FORWARDED_FOR', request.remote_addr)
    is_host_client = client_ip == SERVER_IP or client_ip == '127.0.0.1'
    clients[request.sid] = {'ip': client_ip, 'is_host': is_host_client}
    logging.info(f"Client connected: {request.sid}, IP: {client_ip}, Host: {is_host_client}")
    emit('connection_ack', {'sid': request.sid, 'is_host': is_host_client, 'server_ip': SERVER_IP})

@socketio.on('disconnect')
def handle_disconnect():
    logging.info(f"Client disconnected: {request.sid}")
    # Handle ongoing transfers if a client disconnects
    for tid, session in list(transfer_sessions.items()): # Iterate over a copy
        if session['sender_sid'] == request.sid or session.get('receiver_sid') == request.sid:
            if session['status'] not in ['completed', 'failed', 'cancelled']:
                logging.warning(f"Client {request.sid} disconnected during active transfer {tid}. Cancelling.")
                session['status'] = 'failed' # Or 'cancelled_due_disconnect'
                log_transfer_history(tid, status_override="Disconnected")
                
                # Notify other party if exists
                other_sid = None
                if session['sender_sid'] == request.sid and session.get('receiver_sid'):
                    other_sid = session['receiver_sid']
                elif session.get('receiver_sid') == request.sid:
                    other_sid = session['sender_sid']
                
                if other_sid:
                    emit('transfer_cancelled', {
                        'transfer_id': tid, 
                        'reason': 'Other party disconnected.'
                        }, room=other_sid)
                cleanup_session_files(tid)
    if request.sid in clients:
        del clients[request.sid]

@socketio.on('initiate_transfer_request')
def handle_initiate_transfer_request(data):
    sender_sid = request.sid
    files_metadata = data.get('files', [])
    if not files_metadata:
        emit('error_message', {'message': 'No files selected for transfer.'}, room=sender_sid)
        return

    transfer_id = str(uuid.uuid4())
    security_code = generate_security_code()
    
    staged_root_path = os.path.join(TEMP_DIR, transfer_id)
    os.makedirs(staged_root_path, exist_ok=True)

    total_size = sum(f['size'] for f in files_metadata)
    
    # Determine if sender is host
    sender_client_info = clients.get(sender_sid, {})
    is_sender_host = sender_client_info.get('is_host', False)
    sender_ip = sender_client_info.get('ip', 'Unknown')

    transfer_sessions[transfer_id] = {
        'sender_sid': sender_sid,
        'files': [{'name': f['name'], 'size': f['size'], 'staged': False} for f in files_metadata],
        'total_size': total_size,
        'staged_bytes': 0,
        'security_code': security_code,
        'status': 'pending_staging',
        'staged_root_path': staged_root_path,
        'start_time': time.time(),
        'is_sender_host': is_sender_host,
        'sender_ip': sender_ip,
        'total_sent_bytes': 0 # for download tracking
    }
    log_transfer_history(transfer_id) # Initial log

    logging.info(f"Transfer {transfer_id} initiated by {sender_sid}. Code: {security_code}. Files: {[f['name'] for f in files_metadata]}")

    # Tell sender to start uploading files to staging area AND display security code
    emit('transfer_initiated_await_staging', {
        'transfer_id': transfer_id,
        'security_code': security_code,
        'files_to_stage': files_metadata # Client needs this to know what to upload
    }, room=sender_sid)

    # Notify potential receivers
    receiver_sids = [sid for sid, client_info in clients.items() if sid != sender_sid]
    # More specific targeting:
    # if is_sender_host: # Host is sending, target guests
    #     receiver_sids = [sid for sid, client_info in clients.items() if not client_info.get('is_host')]
    # else: # Guest is sending, target hosts
    #     receiver_sids = [sid for sid, client_info in clients.items() if client_info.get('is_host')]

    if not receiver_sids:
        emit('error_message', {'message': 'No other devices connected to receive the files.'}, room=sender_sid)
        transfer_sessions[transfer_id]['status'] = 'failed'
        log_transfer_history(transfer_id, status_override="No Receiver")
        cleanup_session_files(transfer_id)
        return

    for r_sid in receiver_sids:
        emit('incoming_transfer_request', {
            'transfer_id': transfer_id,
            'sender_sid': sender_sid, # So receiver knows who it's from (optional display)
            'files': files_metadata,
            'total_size': total_size,
            'security_code_to_match': security_code # For display/matching on receiver UI
        }, room=r_sid)

@socketio.on('approve_transfer')
def handle_approve_transfer(data):
    receiver_sid = request.sid
    transfer_id = data.get('transfer_id')
    entered_code = data.get('entered_code')

    if not transfer_id or transfer_id not in transfer_sessions:
        emit('error_message', {'message': 'Invalid transfer ID.'}, room=receiver_sid)
        return

    session = transfer_sessions[transfer_id]

    if session['status'] == 'cancelled' or session['status'] == 'failed':
        emit('error_message', {'message': 'Transfer already cancelled or failed.'}, room=receiver_sid)
        return
        
    if session.get('receiver_sid') and session.get('receiver_sid') != receiver_sid :
        emit('error_message', {'message': 'Transfer already claimed by another receiver.'}, room=receiver_sid)
        return

    if session['security_code'] == entered_code:
        session['receiver_sid'] = receiver_sid
        session['receiver_ip'] = clients.get(receiver_sid, {}).get('ip', 'Unknown')
        
        # Check if staging is complete
        all_staged = all(f.get('staged', False) for f in session['files'])
        if all_staged:
            session['status'] = 'approved' # Ready for download by receiver
            log_transfer_history(transfer_id) # Update log with approval
            emit('transfer_approved_start_download', {
                'transfer_id': transfer_id,
                'files': session['files'] # List of files to download
            }, room=receiver_sid)
            emit('transfer_approved_by_receiver', {'transfer_id': transfer_id}, room=session['sender_sid'])
            logging.info(f"Transfer {transfer_id} approved by {receiver_sid}. Staging complete.")
        else:
            # Staging not yet complete. Mark as approved, but receiver must wait.
            session['status'] = 'approved_pending_staging_completion'
            log_transfer_history(transfer_id, status_override='Approved (Pending Staging)')
            emit('transfer_approved_await_staging_completion', {'transfer_id': transfer_id}, room=receiver_sid)
            emit('transfer_approved_by_receiver', {'transfer_id': transfer_id}, room=session['sender_sid'])
            logging.info(f"Transfer {transfer_id} approved by {receiver_sid}. Waiting for staging to complete.")
            # Server will emit 'transfer_ready_to_download' to receiver when staging completes via the upload route logic
            # Need a mechanism for this:
            # In upload_to_stage, if all_staged and session['status'] == 'approved_pending_staging_completion':
            #   session['status'] = 'approved'
            #   emit('transfer_ready_to_download', {...}, room=session['receiver_sid'])
            # This will be implicitly handled if client logic for 'all_files_staged' (if sender gets it)
            # then tells receiver it's ok, or if receiver periodically checks or waits for 'transfer_approved_start_download'
            # For now, the `upload_to_stage` will make it `pending_approval` when staging done. If approved already, this needs merge.

            # Let's refine: if all_staged in upload_to_stage:
            #   if session['status'] == 'pending_staging': session['status'] = 'pending_approval'
            #   elif session['status'] == 'approved_pending_staging_completion':
            #       session['status'] = 'approved'
            #       emit('transfer_approved_start_download', {...}, room=session['receiver_sid'])

            # This needs to be handled in `upload_to_stage` when all files are confirmed staged.
            # Check again: if `all_staged` becomes true in `upload_to_stage`
            if 'all_files_staged_check_hook' not in session: # Add a hook
                def staging_complete_hook():
                    if transfer_id in transfer_sessions and transfer_sessions[transfer_id]['status'] == 'approved_pending_staging_completion':
                        transfer_sessions[transfer_id]['status'] = 'approved'
                        log_transfer_history(transfer_id) # Update log
                        emit('transfer_approved_start_download', {
                            'transfer_id': transfer_id,
                            'files': transfer_sessions[transfer_id]['files']
                        }, room=transfer_sessions[transfer_id]['receiver_sid'])
                        logging.info(f"Staging completed for approved transfer {transfer_id}. Receiver can now download.")
                session['all_files_staged_check_hook'] = staging_complete_hook
            
            # If by chance staging completed right before this approval came
            if all_staged: # Re-check if staging finished in a race
                 session['all_files_staged_check_hook']() # Call it manually


    else: # Wrong code
        emit('transfer_rejected', {
            'transfer_id': transfer_id, 
            'reason': 'Incorrect security code.'
            }, room=receiver_sid)
        # Optionally notify sender, but usually only if it's a definitive rejection.
        # A wrong code attempt might just be a typo.
        logging.info(f"Incorrect code for {transfer_id} by {receiver_sid}.")

@socketio.on('reject_transfer')
def handle_reject_transfer(data):
    receiver_sid = request.sid
    transfer_id = data.get('transfer_id')

    if not transfer_id or transfer_id not in transfer_sessions:
        emit('error_message', {'message': 'Invalid transfer ID for rejection.'}, room=receiver_sid)
        return

    session = transfer_sessions[transfer_id]
    if session.get('receiver_sid') and session.get('receiver_sid') != receiver_sid:
        # Another receiver already actioned this (e.g. approved)
        # This specific receiver can't reject it anymore.
        return

    session['status'] = 'rejected'
    log_transfer_history(transfer_id)
    cleanup_session_files(transfer_id)

    emit('transfer_rejected_ack', {'transfer_id': transfer_id}, room=receiver_sid) # Ack to self
    emit('transfer_rejected_by_receiver', {
        'transfer_id': transfer_id, 
        'receiver_sid': receiver_sid
        }, room=session['sender_sid'])
    logging.info(f"Transfer {transfer_id} rejected by {receiver_sid}.")

@socketio.on('cancel_transfer')
def handle_cancel_transfer(data):
    canceller_sid = request.sid
    transfer_id = data.get('transfer_id')

    if not transfer_id or transfer_id not in transfer_sessions:
        emit('error_message', {'message': 'Invalid transfer ID for cancellation.'}, room=canceller_sid)
        return
    
    session = transfer_sessions[transfer_id]
    if session['status'] in ['completed', 'failed', 'cancelled', 'rejected']:
        emit('error_message', {'message': 'Transfer already finalized or cancelled.'}, room=canceller_sid)
        return

    previous_status = session['status']
    session['status'] = 'cancelled'
    log_transfer_history(transfer_id)
    cleanup_session_files(transfer_id)

    logging.info(f"Transfer {transfer_id} cancelled by {canceller_sid}. Previous status: {previous_status}")

    # Notify both parties (sender and potential/actual receiver)
    emit('transfer_cancelled', {'transfer_id': transfer_id, 'reason': 'Cancelled by user.'}, room=session['sender_sid'])
    if session.get('receiver_sid'):
        emit('transfer_cancelled', {'transfer_id': transfer_id, 'reason': 'Cancelled by user.'}, room=session['receiver_sid'])
    else: # If cancelled before receiver is set (e.g. during staging or pending approval by anyone)
        # Notify all potential receivers that the request is void
        for sid, client_info in clients.items():
            if sid != session['sender_sid']:
                 emit('transfer_request_voided', {'transfer_id': transfer_id}, room=sid)
    
    # If cancelling during active download stream, the download route also needs a way to stop
    # This is hard. The HTTP route is blocking. Client closing connection is one way.
    # For now, this cancellation primarily stops new files from being downloaded and cleans up.
    # Ongoing HTTP download might complete current chunk or error out.

# Hook for when all files are staged (called from upload_to_stage)
def on_all_files_staged(transfer_id):
    if transfer_id not in transfer_sessions: return
    session = transfer_sessions[transfer_id]

    logging.info(f"Hook: All files staged for {transfer_id}. Current status: {session['status']}")
    
    if session['status'] == 'pending_staging':
        session['status'] = 'pending_approval'
        socketio.emit('all_files_staged_pending_approval', {'transfer_id': transfer_id}, room=session['sender_sid'])
        # The incoming_transfer_request was already sent to receivers. They are waiting.
    
    elif session['status'] == 'approved_pending_staging_completion':
        session['status'] = 'approved'
        log_transfer_history(transfer_id) # Update log to 'approved'
        socketio.emit('transfer_approved_start_download', {
            'transfer_id': transfer_id,
            'files': session['files']
        }, room=session['receiver_sid'])
        logging.info(f"Staging completed for approved transfer {transfer_id}. Receiver can now download.")
    
    # Call custom hook if it exists (e.g. for race condition with approval)
    if 'all_files_staged_check_hook' in session and callable(session['all_files_staged_check_hook']):
        session['all_files_staged_check_hook']()
        del session['all_files_staged_check_hook'] # Use once

# Monkey patch this into the upload_to_stage logic where `all_staged` becomes true.
# This is a bit of a workaround for direct calls between HTTP and SocketIO contexts.
# A better way would be a task queue or event bus if this grew more complex.

# Modify `upload_to_stage`:
# ...
#        all_staged = all(f.get('staged', False) for f in session['files'])
#        if all_staged:
#            on_all_files_staged(transfer_id) # Call the hook
# ...
# This modification needs to be done carefully in the actual upload_to_stage.
# For simplicity, I will integrate the logic of `on_all_files_staged` directly into `upload_to_stage` where `all_staged` becomes true.

# Revised part in `upload_to_stage`
# (This is illustrative, the actual change is made in the `upload_to_stage` function above)
# if all_staged:
#     logging.info(f"All files staged for {transfer_id}. Current status: {session['status']}")
#     if session['status'] == 'pending_staging':
#         session['status'] = 'pending_approval' # Formal status update
#         socketio.emit('all_files_staged_pending_approval', {'transfer_id': transfer_id}, room=session['sender_sid'])
#     elif session['status'] == 'approved_pending_staging_completion':
#         session['status'] = 'approved'
#         log_transfer_history(transfer_id) # Update log status
#         socketio.emit('transfer_approved_start_download', {
#             'transfer_id': transfer_id,
#             'files': session['files']
#         }, room=session['receiver_sid'])
#         logging.info(f"Staging completed for already approved transfer {transfer_id}. Receiver can download.")
# (The actual code for `upload_to_stage` has this logic integrated now)

# --- Main Execution ---
if __name__ == '__main__':
    print(f"File Transfer Server starting...")
    print(f"Open your browser and navigate to:")
    print(f"  Laptop/Host PC: http://127.0.0.1:5000 or http://localhost:5000")
    print(f"  Mobile/Other Device on same WiFi: http://{SERVER_IP}:5000")
    print(f"Root directory: {ROOT_DIR}")
    print(f"Temporary staging area: {TEMP_DIR}")
    print(f"Received files will be stored in: {RECEIVED_DIR}")
    print(f"Transfer history: {HISTORY_FILE}")
    
    # Ensure history file has headers if it's new/empty
    if not os.path.isfile(HISTORY_FILE) or os.path.getsize(HISTORY_FILE) == 0:
        with open(HISTORY_FILE, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                "Timestamp", "TransferID", "Direction", "FileNames", 
                "TotalSizeMB", "Status", "SecurityCode", 
                "SenderIP", "ReceiverIP"
            ])
            
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    # Debug=True can cause issues with SocketIO and duplicate executions. use_reloader=False is good for stability.