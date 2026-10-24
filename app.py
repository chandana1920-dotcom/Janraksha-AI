import os, uuid, base64, smtplib, json, re
import hashlib
import ipaddress
import socket
from urllib.parse import urlsplit
from threading import Lock, Thread, Event
import time
from email.message import EmailMessage
from datetime import datetime, date, timedelta
from functools import wraps
from io import BytesIO

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request, session, send_from_directory, stream_with_context
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from pywebpush import webpush, WebPushException

from backend.app.services.email_service import (
    send_verified_match_email,
    send_case_update_email,
    send_notification_email,
)

def load_local_env():
    """Load simple KEY=VALUE settings from the project .env without external packages."""
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    try:
        with open(env_path, encoding='utf-8') as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith('export '):
                    line = line[7:].lstrip()
                key, separator, value = line.partition('=')
                key = key.strip()
                if not separator or not key:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]
                else:
                    value = value.split(' #', 1)[0].strip()
                os.environ.setdefault(key, value)
    except FileNotFoundError:
        pass


load_local_env()

POLICE_STATIONS = {
    'Vidyaranyapura Police Station': ('vidyaranyapura', 'vidyaranya', 'hebbal', 'jalahalli', 'sahakara nagar'),
    'Yelahanka New Town Police Station': ('yelahanka', 'new town', 'attur', 'rajanukunte', 'bagalur'),
    'Yeshwanthapura Police Station': ('yeshwanthpur', 'yeshwantpur', 'yeshwanthapura', 'yeshwantpura', 'mathikere', 'peenya', 'goraguntepalya'),
}


def missing_person_matches_station(person, station):
    location = re.sub(r'[^a-z0-9 ]', ' ', (person.last_seen_location or '').lower())
    return any(term in location for term in POLICE_STATIONS.get(station, ()))

app = Flask(__name__, static_folder='frontend', static_url_path='')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'development-only-change-me')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///janraksha.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_UPLOAD_MB', 5)) * 1024 * 1024
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER') or os.environ.get('SMTP_HOST') or ''
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', os.environ.get('SMTP_PORT', '587')))
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME') or os.environ.get('SMTP_USER') or ''
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD') or os.environ.get('SMTP_PASSWORD') or ''
app.config['MAIL_USE_TLS'] = str(os.environ.get('MAIL_USE_TLS', os.environ.get('SMTP_TLS', 'true'))).lower() in {'1', 'true', 'yes', 'on'}
app.config['MAIL_USE_SSL'] = str(os.environ.get('MAIL_USE_SSL', 'false')).lower() in {'1', 'true', 'yes', 'on'}
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER') or os.environ.get('SMTP_FROM') or 'JanRaksha AI <noreply@janraksha.local>'
app.config['VAPID_PUBLIC_KEY'] = os.environ.get('VAPID_PUBLIC_KEY', '')
app.config['VAPID_PRIVATE_KEY'] = os.environ.get('VAPID_PRIVATE_KEY', '')
app.config['VAPID_SUBJECT'] = os.environ.get('VAPID_SUBJECT', 'mailto:admin@example.com')
app.config['POTENTIAL_MATCH_THRESHOLD'] = float(os.environ.get('POTENTIAL_MATCH_THRESHOLD', '0.80'))
if not 0 <= app.config['POTENTIAL_MATCH_THRESHOLD'] <= 1:
    raise ValueError('POTENTIAL_MATCH_THRESHOLD must be between 0 and 1.')
db = SQLAlchemy(app)
webcam_tracks = {}
webcam_tracks_lock = Lock()
remote_webcam_captures = {}
remote_webcam_captures_lock = Lock()

ROLES = {'FAMILY_USER', 'CITIZEN', 'INVESTIGATOR', 'ADMIN_DEMO', 'POLICE_OFFICER'}
PUBLIC_REGISTRATION_ROLES = {'FAMILY_USER', 'CITIZEN', 'INVESTIGATOR'}
CASE_STATUSES = {'ACTIVE', 'UNDER REVIEW', 'RELEVANT LEAD', 'PERSON_FOUND', 'PERSON FOUND', 'CLOSED'}
MATCH_STATUSES = {'REQUIRES_HUMAN_VERIFICATION', 'VERIFIED_BY_AUTHORIZED_USER', 'NOT_RELEVANT'}
EMERGENCY_CATEGORIES = {'ACCIDENT', 'FIRE', 'MEDICAL', 'POLICE', 'WOMEN_SAFETY', 'CHILD_SAFETY'}
EMERGENCY_CASE_STATUSES = {'PENDING', 'IN_PROGRESS', 'RESOLVED'}
EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True); name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False, index=True); phone = db.Column(db.String(30))
    password_hash = db.Column(db.String(256), nullable=False); role = db.Column(db.String(30), nullable=False)
    location = db.Column(db.String(180)); created_at = db.Column(db.DateTime, default=datetime.utcnow)


class MissingPerson(db.Model):
    id = db.Column(db.Integer, primary_key=True); case_id = db.Column(db.String(20), unique=True, nullable=False, index=True)
    registered_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False); name = db.Column(db.String(120), nullable=False)
    age = db.Column(db.Integer); gender = db.Column(db.String(30)); photo = db.Column(db.String(255))
    description = db.Column(db.Text); clothing_description = db.Column(db.Text); last_seen_location = db.Column(db.String(200))
    last_seen_date = db.Column(db.Date)
    last_seen_time = db.Column(db.String(10)); status = db.Column(db.String(30), default='ACTIVE'); created_at = db.Column(db.DateTime, default=datetime.utcnow)
    approval_status = db.Column(db.String(30), nullable=False, default='PENDING_APPROVAL', index=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    reviewed_at = db.Column(db.DateTime)
    rejection_reason = db.Column(db.Text)


class FaceEmbedding(db.Model):
    id = db.Column(db.Integer, primary_key=True); missing_person_id = db.Column(db.Integer, db.ForeignKey('missing_person.id'), nullable=False)
    embedding_vector = db.Column(db.Text, nullable=False); model_name = db.Column(db.String(80)); embedding_dimension = db.Column(db.Integer); created_at = db.Column(db.DateTime, default=datetime.utcnow)


class WebcamPerson(db.Model):
    __tablename__ = 'webcam_people'
    id = db.Column(db.Integer, primary_key=True)
    reference_id = db.Column(db.String(24), nullable=False, unique=True, index=True)
    registered_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    age = db.Column(db.Integer)
    gender = db.Column(db.String(30))
    photo = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default='ACTIVE', index=True)
    embedding_vector = db.Column(db.Text, nullable=False)
    embedding_dimension = db.Column(db.Integer, nullable=False)
    model_name = db.Column(db.String(80), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Sighting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.String(20), nullable=False, index=True)
    reported_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    photo = db.Column(db.String(255))
    description = db.Column(db.Text)
    location_name = db.Column(db.String(200))
    sighting_date = db.Column(db.Date)
    sighting_time = db.Column(db.String(10))
    reported_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    status = db.Column(db.String(30), default='SUBMITTED', nullable=False)
    review_status = db.Column(db.String(30), default='PENDING_REVIEW', nullable=False, index=True)


class FoundPerson(db.Model):
    id = db.Column(db.Integer, primary_key=True); reported_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    photo = db.Column(db.String(255)); description = db.Column(db.Text); location_name = db.Column(db.String(200))
    found_date = db.Column(db.Date); status = db.Column(db.String(30), default='SUBMITTED'); matched_case_id = db.Column(db.String(20)); verification_status = db.Column(db.String(30), default='PENDING_HUMAN_REVIEW')
    verified_by = db.Column(db.Integer, db.ForeignKey('user.id')); verified_at = db.Column(db.DateTime); verified_location = db.Column(db.String(200)); verified_found = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AIMatch(db.Model):
    id = db.Column(db.Integer, primary_key=True); source_type = db.Column(db.String(30)); source_id = db.Column(db.Integer)
    missing_person_id = db.Column(db.Integer, db.ForeignKey('missing_person.id')); similarity_score = db.Column(db.Float); combined_score = db.Column(db.Float)
    status = db.Column(db.String(50), default='REQUIRES_HUMAN_VERIFICATION'); created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Timeline(db.Model):
    id = db.Column(db.Integer, primary_key=True); case_id = db.Column(db.String(20), index=True); event_type = db.Column(db.String(80)); description = db.Column(db.Text)
    created_by = db.Column(db.Integer); created_at = db.Column(db.DateTime, default=datetime.utcnow)


class FamilyReport(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.String(20), nullable=False, index=True)
    submitted_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    location_name = db.Column(db.String(200), nullable=False)
    found_date = db.Column(db.Date)
    found_time = db.Column(db.String(10))
    condition = db.Column(db.String(200))
    details = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True); user_id = db.Column(db.Integer, db.ForeignKey('user.id')); title = db.Column(db.String(150)); message = db.Column(db.Text)
    type = db.Column(db.String(30), default='info'); is_read = db.Column(db.Boolean, default=False); created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sighting_id = db.Column(db.Integer, db.ForeignKey('sighting.id'), index=True)
    found_person_id = db.Column(db.Integer, db.ForeignKey('found_person.id'), index=True)


class EmailNotification(db.Model):
    __tablename__ = 'email_notifications'
    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.String(20), index=True)
    match_id = db.Column(db.Integer, db.ForeignKey('ai_match.id'))
    recipient_user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    recipient_email = db.Column(db.String(200))
    notification_type = db.Column(db.String(80), default='VERIFIED_MATCH')
    subject = db.Column(db.String(200))
    status = db.Column(db.String(20), default='PENDING')
    attempt_count = db.Column(db.Integer, default=0)
    sent_at = db.Column(db.DateTime)
    failure_reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AuditLog(db.Model):
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    case_id = db.Column(db.String(20), index=True)
    match_id = db.Column(db.Integer)
    action = db.Column(db.String(80), nullable=False, index=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    ip_address = db.Column(db.String(64))
    meta = db.Column('metadata', db.Text)


class InvestigationNote(db.Model):
    id = db.Column(db.Integer, primary_key=True); case_id = db.Column(db.String(20)); author_id = db.Column(db.Integer); body = db.Column(db.Text); created_at = db.Column(db.DateTime, default=datetime.utcnow)


class WebcamSession(db.Model):
    __tablename__ = 'webcam_sessions'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(40), unique=True, nullable=False, index=True)
    investigator_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    location_name = db.Column(db.String(200))
    monitoring_description = db.Column(db.Text)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    ended_at = db.Column(db.DateTime)
    status = db.Column(db.String(30), default='ACTIVE')
    faces_processed = db.Column(db.Integer, default=0)
    potential_matches = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class WebcamDetection(db.Model):
    __tablename__ = 'webcam_detections'
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(40), db.ForeignKey('webcam_sessions.session_id'), nullable=False, index=True)
    case_id = db.Column(db.String(20), index=True)
    similarity_score = db.Column(db.Float)
    frame_timestamp = db.Column(db.String(40))
    status = db.Column(db.String(40), default='POTENTIAL_MATCH')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class EmergencyContact(db.Model):
    id = db.Column(db.Integer, primary_key=True); name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(40), nullable=False); region = db.Column(db.String(160)); instructions = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)


class TrustedContact(db.Model):
    id = db.Column(db.Integer, primary_key=True); user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False); email = db.Column(db.String(160)); phone = db.Column(db.String(40)); created_at = db.Column(db.DateTime, default=datetime.utcnow)


class EmergencyEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True); user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    status = db.Column(db.String(20), default='ACTIVE')
    location_name = db.Column(db.String(200)); message = db.Column(db.Text); created_at = db.Column(db.DateTime, default=datetime.utcnow); cancelled_at = db.Column(db.DateTime)


class PushSubscription(db.Model):
    __tablename__ = 'push_subscriptions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    endpoint_hash = db.Column(db.String(64), nullable=False, unique=True)
    subscription_json = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class PoliceOfficer(db.Model):
    __tablename__ = 'police_officers'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, unique=True)
    police_id = db.Column(db.String(40), nullable=False, unique=True, index=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class EmergencyCase(db.Model):
    __tablename__ = 'emergency_cases'
    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.String(32), nullable=False, unique=True, index=True)
    submission_key = db.Column(db.String(64), unique=True, index=True)
    category = db.Column(db.String(30), nullable=False, index=True)
    reporter_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    reporter_name = db.Column(db.String(120), nullable=False)
    reporter_contact = db.Column(db.String(160))
    description = db.Column(db.Text, nullable=False)
    location_name = db.Column(db.String(200), nullable=False)
    attachment = db.Column(db.String(255))
    status = db.Column(db.String(20), nullable=False, default='PENDING', index=True)
    assigned_officer_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


def normalize_match_status(value):
    return (value or '').upper().replace(' ', '_')


def normalize_case_status(value):
    return (value or '').upper().replace(' ', '_')


def display_case_status(value):
    value = normalize_case_status(value)
    return value.replace('_', ' ')


def display_match_status(value):
    return normalize_match_status(value).replace('_', ' ')


def add_audit_log(user_id=None, case_id=None, match_id=None, action='ACTION', metadata=None, ip_address=None):
    db.session.add(AuditLog(user_id=user_id, case_id=case_id, match_id=match_id, action=action, timestamp=datetime.utcnow(), ip_address=ip_address, meta=json.dumps(metadata or {})))


def api_error(message, status=400):
    return jsonify(error=message), status


def current_user():
    return db.session.get(User, session.get('user_id')) if session.get('user_id') else None


def require_auth(roles=None):
    def outer(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                return api_error('Authentication required.', 401)
            if roles and user.role not in roles:
                return api_error('You do not have permission.', 403)
            return fn(*args, **kwargs)
        return wrapped
    return outer


def user_json(u):
    return {'id': u.id, 'name': u.name, 'email': u.email, 'phone': u.phone, 'role': u.role, 'location': u.location}


def person_json(p):
    displayed_status = p.status if p.approval_status == 'APPROVED' else p.approval_status
    return {'id': p.id, 'case_id': p.case_id, 'name': p.name, 'age': p.age, 'gender': p.gender, 'photo': p.photo,
            'description': p.description, 'clothing_description': p.clothing_description, 'last_seen_location': p.last_seen_location,
            'last_seen_date': p.last_seen_date.isoformat() if p.last_seen_date else None,
            'last_seen_time': p.last_seen_time, 'status': displayed_status, 'approval_status': p.approval_status,
            'reviewed_at': p.reviewed_at.isoformat() if p.reviewed_at else None,
            'rejection_reason': p.rejection_reason, 'created_at': p.created_at.isoformat()}


def webcam_person_json(person):
    return {
        'id': person.id,
        'reference_id': person.reference_id,
        'name': person.name,
        'age': person.age,
        'gender': person.gender,
        'photo': person.photo,
        'description': person.description,
        'status': person.status,
        'created_at': person.created_at.isoformat(),
    }


def next_case_id():
    yr = datetime.utcnow().year; prefix = f'JR-{yr}-'; last = MissingPerson.query.filter(MissingPerson.case_id.like(prefix + '%')).order_by(MissingPerson.case_id.desc()).first()
    n = int(last.case_id[-4:]) + 1 if last else 1; return f'{prefix}{n:04d}'


def next_webcam_reference_id():
    year = datetime.utcnow().year
    prefix = f'WCR-{year}-'
    last = WebcamPerson.query.filter(WebcamPerson.reference_id.like(prefix + '%')).order_by(WebcamPerson.reference_id.desc()).first()
    sequence = int(last.reference_id[-5:]) + 1 if last else 1
    return f'{prefix}{sequence:05d}'


def save_file(file):
    if not file or not file.filename:
        return None
    ext = os.path.splitext(secure_filename(file.filename))[1].lower()
    if ext not in {'.jpg', '.jpeg', '.png'}:
        raise ValueError('Only JPG, JPEG and PNG images are accepted.')
    name = f'{uuid.uuid4().hex}{ext}'
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], name))
    return f'/uploads/{name}'


def save_sighting_photo(file):
    if not file or not file.filename:
        return None
    ext = os.path.splitext(secure_filename(file.filename))[1].lower()
    if ext not in {'.jpg', '.jpeg', '.png'}:
        raise ValueError('Only JPG, JPEG and PNG images are accepted.')
    name = f'{uuid.uuid4().hex}{ext}'
    folder = os.path.join(app.config['UPLOAD_FOLDER'], 'sightings')
    os.makedirs(folder, exist_ok=True)
    file.save(os.path.join(folder, name))
    return f'sightings/{name}'


def save_emergency_attachment(file):
    if not file or not file.filename:
        return None
    ext = os.path.splitext(secure_filename(file.filename))[1].lower()
    if ext not in {'.jpg', '.jpeg', '.png'}:
        raise ValueError('Only JPG, JPEG and PNG images are accepted.')
    name = f'{uuid.uuid4().hex}{ext}'
    folder = os.path.join(app.config['UPLOAD_FOLDER'], 'emergency-cases')
    os.makedirs(folder, exist_ok=True)
    file.save(os.path.join(folder, name))
    return os.path.join('emergency-cases', name)


def next_webcam_session_id():
    prefix = f'WC-{datetime.utcnow().year}-'
    last = WebcamSession.query.filter(WebcamSession.session_id.like(prefix + '%')).order_by(WebcamSession.session_id.desc()).first()
    n = int(last.session_id[-5:]) + 1 if last else 1
    return f'{prefix}{n:05d}'


def _safe_haar_cascade(model_name):
    if not hasattr(cv2, 'CascadeClassifier'):
        return None
    data_dir = getattr(cv2, 'data', None)
    cascade_candidates = []
    if data_dir is not None and hasattr(data_dir, 'haarcascades'):
        cascade_path = os.path.join(data_dir.haarcascades, model_name)
        if os.path.exists(cascade_path):
            cascade_candidates.append(cascade_path)
    cascade_candidates.append(None)
    for candidate in cascade_candidates:
        try:
            cascade = cv2.CascadeClassifier(candidate) if candidate else cv2.CascadeClassifier()
        except TypeError:
            continue
        except Exception:
            continue
        if cascade is None:
            continue
        if hasattr(cascade, 'empty') and callable(cascade.empty):
            try:
                if cascade.empty():
                    continue
            except Exception:
                pass
        return cascade
    return None


WEBCAM_FACE_MODEL = 'opencv-sface-yunet-v1'
# Use a conservative webcam threshold. OpenCV's 0.363 is a generic benchmark
# cutoff; it allowed visibly incorrect webcam leads in this application's
# camera/reference conditions. This threshold maps to an 80% display score.
WEBCAM_FACE_COSINE_THRESHOLD = 0.60
_webcam_face_models = None
_webcam_face_models_lock = Lock()


def _get_webcam_face_models():
    """Load the official OpenCV Zoo YuNet detector and SFace recognizer once."""
    global _webcam_face_models
    if _webcam_face_models is not None:
        return _webcam_face_models
    with _webcam_face_models_lock:
        if _webcam_face_models is not None:
            return _webcam_face_models
        model_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models')
        detector_path = os.path.join(model_dir, 'face_detection_yunet_2023mar.onnx')
        recognizer_path = os.path.join(model_dir, 'face_recognition_sface_2021dec.onnx')
        if not os.path.isfile(detector_path) or not os.path.isfile(recognizer_path):
            raise ValueError('Webcam face recognition models are missing. Restore the models folder and restart JanRaksha.')
        if not hasattr(cv2, 'FaceDetectorYN') or not hasattr(cv2, 'FaceRecognizerSF'):
            raise ValueError('Webcam face recognition needs OpenCV 4.10 or newer. Install requirements.txt and restart JanRaksha.')
        try:
            # Video frames can be compressed, motion-blurred, and much smaller
            # than registration photos. A lower detector cutoff catches those
            # faces; quality checks and the SFace match threshold still reject
            # weak identity evidence later in the pipeline.
            detector = cv2.FaceDetectorYN.create(detector_path, '', (320, 320), 0.65, 0.3, 5000)
            recognizer = cv2.FaceRecognizerSF.create(recognizer_path, '')
        except cv2.error as error:
            raise ValueError(f'Could not load the webcam face recognition models: {error}') from error
        _webcam_face_models = (detector, recognizer)
        return _webcam_face_models


def _detect_webcam_faces(image):
    detector, _ = _get_webcam_face_models()
    try:
        detector.setInputSize((image.shape[1], image.shape[0]))
        _, rows = detector.detect(image)
    except cv2.error as error:
        raise ValueError(f'Could not detect faces in this image: {error}') from error
    if rows is None:
        return []
    results = []
    for row in rows:
        x, y, width, height = [int(round(value)) for value in row[:4]]
        if width < 20 or height < 20:
            continue
        results.append({
            'x': max(0, x), 'y': max(0, y), 'width': width, 'height': height,
            # FaceRecognizerSF.alignCrop expects YuNet's complete 15-value
            # row (box, five landmarks, and detector confidence).
            'landmarks': row.astype(np.float32),
            'detector_confidence': float(row[14]),
        })
    return results


def _webcam_face_embedding(image, face):
    landmarks = face.get('landmarks') if isinstance(face, dict) else None
    if landmarks is None:
        raise ValueError('Face landmarks are missing. Capture the registration photo again.')
    _, recognizer = _get_webcam_face_models()
    try:
        aligned = recognizer.alignCrop(image, np.asarray(landmarks, dtype=np.float32))
        feature = recognizer.feature(aligned).reshape(-1).astype(np.float32)
    except cv2.error as error:
        raise ValueError('Could not align this face. Use a clear, front-facing photo.') from error
    norm = float(np.linalg.norm(feature))
    if feature.size != 128 or norm <= 1e-8:
        raise ValueError('The face recognition model returned an invalid feature vector.')
    return feature / norm


def detect_multiple_faces(frame_path):
    image = cv2.imread(frame_path)
    if image is None:
        raise ValueError('The webcam frame could not be read.')
    # YuNet provides the facial landmarks required to align faces consistently
    # for SFace embeddings at both registration and live matching time.
    return _detect_webcam_faces(image)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    frontal_cascade = _safe_haar_cascade('haarcascade_frontalface_default.xml')
    frontal_alt_cascade = _safe_haar_cascade('haarcascade_frontalface_alt2.xml')
    if frontal_cascade is None and frontal_alt_cascade is None:
        version = getattr(cv2, '__version__', 'unknown')
        raise ValueError(
            f'Face scanning requires OpenCV 4.x with Haar support (detected OpenCV {version}). '
            r'Run JanRaksha with .venv\Scripts\python.exe after installing requirements.txt.'
        )
    detected = []
    # Run two frontal Haar models: the alternate model often finds faces at
    # slightly different lighting/expressions. Profile models below cover
    # side views. The overlap pass merges duplicate boxes from these models.
    minimum_face_size = max(64, int(min(gray.shape[:2]) * 0.12))
    for cascade in (frontal_cascade, frontal_alt_cascade):
        if cascade is None:
            continue
        frontal = cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=7,
            minSize=(minimum_face_size, minimum_face_size),
        )
        detected.extend((int(x), int(y), int(w), int(h)) for x, y, w, h in frontal)

    # Haar profile models cover side views; flip the image to detect profiles
    # facing the other direction, then map those boxes back to frame coordinates.
    profile_cascade = _safe_haar_cascade('haarcascade_profileface.xml')
    if profile_cascade is not None:
        for profile_gray, mirrored in ((gray, False), (cv2.flip(gray, 1), True)):
            profiles = profile_cascade.detectMultiScale(
                profile_gray, scaleFactor=1.1, minNeighbors=6,
                minSize=(minimum_face_size, minimum_face_size),
            )
            for x, y, width, height in profiles:
                x, y, width, height = int(x), int(y), int(width), int(height)
                if mirrored:
                    x = gray.shape[1] - x - width
                detected.append((x, y, width, height))

    # Merge overlapping frontal/profile detections so one face gets one box.
    unique = []
    for x, y, width, height in sorted(detected, key=lambda box: box[2] * box[3], reverse=True):
        duplicate = False
        for ux, uy, uw, uh in unique:
            intersection_width = max(0, min(x + width, ux + uw) - max(x, ux))
            intersection_height = max(0, min(y + height, uy + uh) - max(y, uy))
            intersection = intersection_width * intersection_height
            union = width * height + uw * uh - intersection
            if union and intersection / union >= 0.35:
                duplicate = True
                break
        if not duplicate:
            unique.append((x, y, width, height))
    return [
        {'x': x, 'y': y, 'width': width, 'height': height}
        for x, y, width, height in unique
    ]


def _expanded_face_crop(image, face):
    image_height, image_width = image.shape[:2]
    x, y = face['x'], face['y']
    width, height = face['width'], face['height']
    margin_x = round(width * 0.15)
    margin_y = round(height * 0.15)
    left = max(0, x - margin_x)
    top = max(0, y - margin_y)
    right = min(image_width, x + width + margin_x)
    bottom = min(image_height, y + height + margin_y)
    return image[top:bottom, left:right]


def _face_quality_metrics(face_image):
    if face_image is None or face_image.size == 0:
        return {'ok': False, 'reason': 'The detected face could not be read.', 'quality_score': 0.0, 'pose_score': 0.0}
    if len(face_image.shape) == 3:
        gray = cv2.cvtColor(face_image, cv2.COLOR_BGR2GRAY)
    else:
        gray = face_image
    height, width = gray.shape[:2]
    if width < 64 or height < 64:
        return {'ok': False, 'reason': 'The detected face is too small.', 'quality_score': 0.0, 'pose_score': 0.0}
    if not 0.55 <= width / height <= 1.45:
        return {'ok': False, 'reason': 'The detected face geometry is distorted.', 'quality_score': 0.0, 'pose_score': 0.0}
    brightness = float(np.mean(gray))
    exposure = float(np.mean(gray >= 245))
    blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    detail = float(np.std(gray))
    quality_score = 1.0
    if brightness < 35:
        quality_score *= 0.35
    if exposure > 0.7:
        quality_score *= 0.40
    if blur < 20:
        quality_score *= 0.55
    if detail < 18:
        quality_score *= 0.55
    eye_cascade = _safe_haar_cascade('haarcascade_eye_tree_eyaglasses.xml')
    pose_score = 1.0
    if eye_cascade is not None:
        upper_face = gray[:max(1, int(height * 0.7)), :]
        eyes = eye_cascade.detectMultiScale(upper_face, scaleFactor=1.1, minNeighbors=4, minSize=(8, 8))
        if len(eyes) < 2:
            pose_score = 0.55
            quality_score *= 0.85
    else:
        pose_score = 0.5
        quality_score *= 0.75
    quality_score = max(0.0, min(1.0, quality_score))
    ok = quality_score >= 0.15 and pose_score >= 0.20
    if not ok:
        if brightness < 35:
            reason = 'The detected face is too dark.'
        elif exposure > 0.7:
            reason = 'The detected face is overexposed.'
        elif blur < 20:
            reason = 'The detected face is too blurred.'
        elif detail < 18:
            reason = 'The detected face has insufficient detail.'
        else:
            reason = 'Facial features are obscured or not sufficiently visible.'
        return {'ok': False, 'reason': reason, 'quality_score': quality_score, 'pose_score': pose_score}
    return {'ok': True, 'reason': None, 'quality_score': quality_score, 'pose_score': pose_score}


def assess_face_quality(face_image):
    metrics = _face_quality_metrics(face_image)
    return metrics['ok'], metrics['reason']


def _alignment_and_resize(face_image):
    image = face_image.copy()
    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image
    if gray.shape[0] == 0 or gray.shape[1] == 0:
        return gray.astype(np.float32)
    eye_cascade = _safe_haar_cascade('haarcascade_eye_tree_eyeglasses.xml')
    eyes = []
    if eye_cascade is not None:
        try:
            eyes = eye_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(10, 10))
        except Exception:
            eyes = []
    if len(eyes) >= 2:
        eyes = sorted(eyes, key=lambda item: item[0])
        left_eye = eyes[0]
        right_eye = eyes[-1]
        left_center = (left_eye[0] + left_eye[2] / 2, left_eye[1] + left_eye[3] / 2)
        right_center = (right_eye[0] + right_eye[2] / 2, right_eye[1] + right_eye[3] / 2)
        angle = np.degrees(np.arctan2(right_center[1] - left_center[1], right_center[0] - left_center[0]))
        rotation_matrix = cv2.getRotationMatrix2D((gray.shape[1] / 2, gray.shape[0] / 2), -angle, 1.0)
        image = cv2.warpAffine(image, rotation_matrix, (gray.shape[1], gray.shape[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image
    gray = cv2.equalizeHist(gray)
    resized = cv2.resize(gray, (96, 96), interpolation=cv2.INTER_AREA)
    resized = resized.astype(np.float32)
    resized = (resized - resized.mean()) / (resized.std() + 1e-6)
    return resized


def _reference_agreement(similarity_score):
    if similarity_score >= 92:
        return 1.0
    if similarity_score >= 82:
        return 0.8
    if similarity_score >= 72:
        return 0.6
    return 0.4


def _face_match_evidence(similarity_score, quality_score, pose_score, reference_score=1.0, temporal_score=1.0, context_score=0.8):
    combined = (
        similarity_score * 0.45 +
        quality_score * 100 * 0.25 +
        pose_score * 100 * 0.15 +
        reference_score * 100 * 0.1 +
        temporal_score * 100 * 0.05 +
        context_score * 100 * 0.05
    )
    return float(np.clip(combined, 0, 100))


def _bbox_iou(first, second):
    left = max(first['x'], second['x'])
    top = max(first['y'], second['y'])
    right = min(first['x'] + first['width'], second['x'] + second['width'])
    bottom = min(first['y'] + first['height'], second['y'] + second['height'])
    intersection = max(0, right - left) * max(0, bottom - top)
    union = first['width'] * first['height'] + second['width'] * second['height'] - intersection
    return intersection / union if union else 0


def assign_webcam_face_tracks(session_id, faces):
    now = datetime.utcnow()
    with webcam_tracks_lock:
        state = webcam_tracks.setdefault(session_id, {'next_id': 1, 'faces': {}})
        tracked_faces = state['faces']
        for face_id, track in list(tracked_faces.items()):
            if (now - track['last_seen']).total_seconds() > 10:
                del tracked_faces[face_id]

        assignments = []
        unused = set(tracked_faces)
        for bbox in faces:
            candidates = []
            for existing_id in unused:
                previous = tracked_faces[existing_id]['bbox']
                overlap = _bbox_iou(bbox, previous)
                center_x = bbox['x'] + bbox['width'] / 2
                center_y = bbox['y'] + bbox['height'] / 2
                previous_x = previous['x'] + previous['width'] / 2
                previous_y = previous['y'] + previous['height'] / 2
                scale = max(bbox['width'], bbox['height'], previous['width'], previous['height'])
                distance = np.hypot(center_x - previous_x, center_y - previous_y) / max(scale, 1)
                area_ratio = max(
                    bbox['width'] * bbox['height'],
                    previous['width'] * previous['height'],
                ) / max(min(
                    bbox['width'] * bbox['height'],
                    previous['width'] * previous['height'],
                ), 1)
                if overlap >= 0.1 or (distance <= 1.25 and area_ratio <= 2):
                    candidates.append((existing_id, max(overlap, 0.1 * (1.25 - distance))))
            face_id, _ = max(candidates, key=lambda candidate: candidate[1], default=(None, 0))
            if face_id is None:
                face_id = f'face_{state["next_id"]:02d}'
                state['next_id'] += 1
                tracked_faces[face_id] = {'bbox': bbox, 'last_seen': now, 'result': None, 'last_processed': None}
            else:
                unused.remove(face_id)
                track = tracked_faces[face_id]
                track['bbox'] = bbox
                track['last_seen'] = now

            track = tracked_faces[face_id]
            cached_result = track['result']
            processed_at = track['last_processed']
            cache_valid = cached_result is not None and (
                cached_result['status'] == 'POTENTIAL_MATCH'
                or (processed_at and (now - processed_at).total_seconds() < 8)
            )
            assignments.append((face_id, cached_result if cache_valid else None))
        return assignments


def cache_webcam_face_result(session_id, face_id, result):
    with webcam_tracks_lock:
        track = webcam_tracks.get(session_id, {}).get('faces', {}).get(face_id)
        if track is not None:
            track['result'] = result
            track['last_processed'] = datetime.utcnow()


def invalidate_webcam_case_cache(case_id):
    with webcam_tracks_lock:
        for state in webcam_tracks.values():
            for track in state.get('faces', {}).values():
                cached = track.get('result') or {}
                candidate_ids = {cached.get('case_id'), cached.get('reference_id')}
                candidate_ids.update(
                    (match.get('case') or {}).get('reference_id')
                    for match in cached.get('ranked_matches', [])
                )
                if case_id in candidate_ids:
                    track['result'] = None


def generate_embedding(face_path):
    image = cv2.imread(face_path) if isinstance(face_path, str) else face_path
    if image is None:
        raise ValueError('The face crop could not be read.')
    normalized = _alignment_and_resize(image)
    vector = normalized.flatten()
    norm = np.linalg.norm(vector)
    if norm <= 1e-6:
        vector = np.full(vector.shape, 1 / np.sqrt(vector.size), dtype=np.float32)
    else:
        vector = vector / norm
    return vector.astype(np.float32)


def search_missing_persons(embedding, source_type=None, source_id=None, similarity_threshold=55.0,
                          record_matches=True, include_low_confidence=False):
    vector = np.asarray(embedding, dtype=np.float32)
    grouped = {}
    for row in FaceEmbedding.query.all():
        person = db.session.get(MissingPerson, row.missing_person_id)
        if not person or person.approval_status != 'APPROVED' or normalize_case_status(person.status) == 'CLOSED':
            continue
        stored = np.array(json.loads(row.embedding_vector), dtype=np.float32)
        if stored.size != vector.size:
            image_name = os.path.basename(person.photo or '')
            image_path = os.path.join(app.config['UPLOAD_FOLDER'], image_name)
            if not image_name or not os.path.isfile(image_path):
                continue
            try:
                stored = embedding_from_file(image_path)
            except ValueError as error:
                app.logger.warning(
                    'Could not refresh the saved face reference for case %s: %s',
                    person.case_id,
                    error,
                )
                continue
            row.embedding_vector = json.dumps(stored.tolist())
            row.model_name = 'opencv-normalized-face-descriptor'
            row.embedding_dimension = int(stored.size)
        score = float(np.clip(np.dot(vector, stored), -1, 1))
        similarity = min(99.9, round((score + 1) * 50, 1))
        if similarity < similarity_threshold:
            continue
        entry = grouped.setdefault(person.id, {'person': person, 'best_similarity': 0.0, 'best_evidence': 0.0})
        evidence = _face_match_evidence(similarity, 0.9, 0.9, _reference_agreement(similarity), 1.0, 0.8)
        entry['best_similarity'] = max(entry['best_similarity'], similarity)
        entry['best_evidence'] = max(entry['best_evidence'], evidence)
    results = []
    for entry in grouped.values():
        if entry['best_evidence'] >= 80 or include_low_confidence:
            results.append({'person': entry['person'], 'similarity': round(entry['best_similarity'], 1), 'combined_score': round(entry['best_evidence'], 1)})
    rank_key = 'similarity' if source_type == 'WEBCAM' else 'combined_score'
    results.sort(key=lambda item: item[rank_key], reverse=True)
    results = results[:10]
    if record_matches:
        for result in results:
            db.session.add(AIMatch(source_type=source_type, source_id=source_id, missing_person_id=result['person'].id,
                                  similarity_score=result['similarity'], combined_score=result['combined_score'],
                                  status='REQUIRES_HUMAN_VERIFICATION'))
    return results


def embedding_from_file(path):
    image = cv2.imread(path)
    if image is None:
        raise ValueError('The uploaded image could not be read.')
    faces = detect_multiple_faces(path)
    if not faces:
        raise ValueError('No face was detected in the registration photo. Use a clear, front-facing portrait.')
    return generate_embedding(_expanded_face_crop(image, faces[0]))


def webcam_embedding_from_file(path):
    image = cv2.imread(path)
    if image is None:
        raise ValueError('The uploaded image could not be read.')
    faces = _detect_webcam_faces(image)
    if not faces:
        raise ValueError('No face was detected. Use a clear, front-facing portrait.')
    if len(faces) > 1:
        raise ValueError('More than one face was detected. Register a photo that shows only this person.')
    face = faces[0]
    crop = _expanded_face_crop(image, face)
    quality = _face_quality_metrics(crop)
    if not quality['ok']:
        raise ValueError(quality['reason'] + ' Use a clear, front-facing portrait.')
    return _webcam_face_embedding(image, face)


def search_embedding(vector, source_type=None, source_id=None):
    return search_missing_persons(vector, source_type=source_type, source_id=source_id)


def search_webcam_people(vector, user_id):
    query = np.asarray(vector, dtype=np.float32)
    matches = []
    if user_id is None:
        return matches
    for person in WebcamPerson.query.filter_by(status='ACTIVE', registered_by=user_id).all():
        try:
            reference = np.asarray(json.loads(person.embedding_vector), dtype=np.float32)
        except (TypeError, ValueError, json.JSONDecodeError):
            app.logger.warning('Skipping invalid webcam reference embedding %s.', person.reference_id)
            continue
        if reference.size != query.size or person.model_name != WEBCAM_FACE_MODEL:
            photo_path = os.path.join(app.config['UPLOAD_FOLDER'], os.path.basename(person.photo or ''))
            if not person.photo or not os.path.isfile(photo_path):
                continue
            try:
                reference = webcam_embedding_from_file(photo_path)
            except ValueError as error:
                app.logger.warning('Could not refresh webcam reference %s: %s', person.reference_id, error)
                continue
            person.embedding_vector = json.dumps(reference.tolist())
            person.embedding_dimension = int(reference.size)
            person.model_name = WEBCAM_FACE_MODEL
            db.session.flush()
        if reference.size != query.size:
            continue
        cosine = float(np.clip(np.dot(query, reference), -1, 1))
        similarity = min(99.9, round((cosine + 1) * 50, 1))
        matches.append({'person': person, 'similarity': similarity, 'cosine': cosine})
    matches.sort(key=lambda match: match['similarity'], reverse=True)
    return matches


def match_chart_data(matches):
    if not matches:
        return None
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import pandas as pd
    import seaborn as sns

    rows = [{'case_id': row['person'].case_id, 'score': row['similarity']} for row in matches]
    chart_data = pd.DataFrame(rows).sort_values('score', ascending=True)
    figure, axis = plt.subplots(figsize=(8, max(2.2, len(rows) * 0.48)))
    sns.barplot(data=chart_data, x='score', y='case_id', color='#198754', orient='h', ax=axis)
    axis.set(xlim=(0, 115), xlabel='Similarity score (%)', ylabel='Case ID', title='Ranked identification leads')
    for bars in axis.containers:
        axis.bar_label(bars, labels=[f'{bar.get_width():.1f}%' for bar in bars], padding=4, fontsize=9)
    axis.grid(axis='x', alpha=0.2)
    figure.tight_layout()
    output = BytesIO(); figure.savefig(output, format='png', dpi=140, bbox_inches='tight'); plt.close(figure)
    encoded = base64.b64encode(output.getvalue()).decode('ascii')
    return f'data:image/png;base64,{encoded}'


def apply_exclusion(matches, exclude_case_id=None):
    if not exclude_case_id:
        return matches
    return [m for m in matches if m.get('person') and m['person'].case_id != exclude_case_id]


def add_timeline(case_id, event, desc, user_id=None):
    db.session.add(Timeline(case_id=case_id, event_type=event, description=desc, created_by=user_id))


def notify_case(case_id, title, message, user_id=None, notification_type='case'):
    p = MissingPerson.query.filter_by(case_id=case_id).first()
    if p:
        target_user = user_id if user_id is not None else p.registered_by
        db.session.add(Notification(user_id=target_user, title=title, message=message, type=notification_type))


def send_email(recipient, subject, message):
    """Sends only when SMTP is configured; in-app notification is always retained."""
    host = app.config.get('MAIL_SERVER') or os.environ.get('SMTP_HOST')
    sender = app.config.get('MAIL_DEFAULT_SENDER') or os.environ.get('SMTP_FROM')
    username = app.config.get('MAIL_USERNAME') or os.environ.get('SMTP_USER')
    password = app.config.get('MAIL_PASSWORD') or os.environ.get('SMTP_PASSWORD')
    if not recipient or not host or not sender:
        return False
    try:
        email = EmailMessage(); email['Subject'] = subject; email['From'] = sender; email['To'] = recipient; email.set_content(message)
        smtp_client = smtplib.SMTP_SSL if app.config.get('MAIL_USE_SSL') else smtplib.SMTP
        with smtp_client(host, int(app.config.get('MAIL_PORT') or os.environ.get('SMTP_PORT', '587')), timeout=10) as client:
            if not app.config.get('MAIL_USE_SSL') and (app.config.get('MAIL_USE_TLS') or str(os.environ.get('SMTP_TLS', 'true')).lower() in {'1', 'true', 'yes', 'on'}):
                client.starttls()
            if username:
                client.login(username, password or '')
            client.send_message(email)
        return True
    except Exception:
        app.logger.warning('Could not deliver email alert.', exc_info=True)
        return False


def send_web_push(user_ids, title, message, url='/?page=emergency'):
    private_key = app.config.get('VAPID_PRIVATE_KEY')
    if not app.config.get('VAPID_PUBLIC_KEY') or not private_key:
        return {'configured': False, 'sent': 0, 'total': 0}
    subscriptions = PushSubscription.query.filter(PushSubscription.user_id.in_(set(user_ids))).all() if user_ids else []
    payload = json.dumps({'title': title, 'body': message, 'url': url})
    sent = 0
    stale = []
    for subscription in subscriptions:
        try:
            webpush(
                subscription_info=json.loads(subscription.subscription_json),
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={'sub': app.config['VAPID_SUBJECT']},
                ttl=300,
            )
            sent += 1
        except WebPushException as exc:
            response = getattr(exc, 'response', None)
            if response is not None and response.status_code in (404, 410):
                stale.append(subscription)
            app.logger.warning('Could not deliver a phone push notification (HTTP %s).', getattr(response, 'status_code', 'unknown'))
        except Exception:
            app.logger.warning('Could not deliver a phone push notification.', exc_info=True)
    for subscription in stale:
        db.session.delete(subscription)
    if stale:
        db.session.commit()
    return {'configured': True, 'sent': sent, 'total': len(subscriptions)}


def send_case_email(case_id, subject, message):
    p = MissingPerson.query.filter_by(case_id=case_id).first(); owner = db.session.get(User, p.registered_by) if p else None
    return send_email(owner.email if owner else None, subject, message + '\n\nThis is an AI-assisted potential lead. It requires authorized human verification.')


def send_app_index():
    response = send_from_directory('frontend', 'index.html')
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response


@app.get('/')
def index():
    return send_app_index()


@app.get('/police')
def police_portal_page():
    return send_app_index()


@app.get('/investigator/matches/<int:match_id>')
def investigator_match_page(match_id):
    return send_app_index()


def can_access_sighting(report, user):
    if not report or not user:
        return False
    if user.role in {'POLICE_OFFICER', 'ADMIN_DEMO'} or report.reported_by == user.id:
        return True
    case = MissingPerson.query.filter_by(case_id=report.case_id).first()
    return bool(user.role == 'FAMILY_USER' and case and case.registered_by == user.id)


def can_access_found_person(report, user):
    if not report or not user:
        return False
    if user.role in {'POLICE_OFFICER', 'ADMIN_DEMO'} or report.reported_by == user.id:
        return True
    case = MissingPerson.query.filter_by(case_id=report.matched_case_id).first() if report.matched_case_id else None
    return bool(user.role == 'FAMILY_USER' and case and case.registered_by == user.id)


def sighting_json(report):
    return {
        'id': report.id,
        'case_id': report.case_id,
        'location_name': report.location_name,
        'sighting_date': report.sighting_date.isoformat() if report.sighting_date else None,
        'sighting_time': report.sighting_time,
        'description': report.description,
        'photo_url': f'/api/sightings/{report.id}/photo' if report.photo else None,
        'submitted_at': report.reported_at.isoformat() if report.reported_at else None,
        'submission_status': report.status,
        'review_status': report.review_status or 'PENDING_REVIEW',
    }


@app.get('/uploads/<path:name>')
def uploads(name):
    normalized_name = name.replace('\\', '/').lstrip('/')
    if normalized_name.startswith(('emergency-cases/', 'sightings/')):
        return api_error('Resource not found.', 404)
    missing_person = MissingPerson.query.filter_by(photo=f'/uploads/{name}').first()
    if missing_person and missing_person.approval_status != 'APPROVED':
        viewer = current_user()
        if not viewer or not (viewer.role in {'POLICE_OFFICER', 'ADMIN_DEMO', 'INVESTIGATOR'} or (viewer.role == 'FAMILY_USER' and missing_person.registered_by == viewer.id)):
            return api_error('Resource not found.', 404)
    sighting = Sighting.query.filter_by(photo=f'/uploads/{name}').first()
    if sighting and not can_access_sighting(sighting, current_user()):
        return api_error('Resource not found.', 404)
    webcam_reference = WebcamPerson.query.filter_by(photo=f'/uploads/{name}').first()
    if webcam_reference:
        user = current_user()
        if not user or webcam_reference.registered_by != user.id:
            return api_error('Resource not found.', 404)
    return send_from_directory(app.config['UPLOAD_FOLDER'], name)


@app.post('/api/auth/register')
def register():
    d = request.get_json() or {}; email = (d.get('email') or '').strip().lower(); role = d.get('role', 'FAMILY_USER')
    if not all([d.get('name'), email, d.get('password'), d.get('terms')]):
        return api_error('Name, email, password and terms acceptance are required.')
    if role not in PUBLIC_REGISTRATION_ROLES:
        return api_error('Choose Family member, Citizen, or Investigator.')
    if User.query.filter_by(email=email).first():
        return api_error('An account with that email already exists.', 409)
    u = User(name=d['name'].strip(), email=email, phone=d.get('phone'), password_hash=generate_password_hash(d['password']), role=role, location=d.get('location'))
    db.session.add(u); db.session.commit(); session['user_id'] = u.id; return jsonify(user=user_json(u)), 201


@app.post('/api/police/login')
def police_login():
    data = request.get_json(silent=True) or {}
    identifier = str(data.get('identifier') or '').strip()
    password = data.get('password') or ''
    police_station = str(data.get('police_station') or '').strip()
    if police_station not in POLICE_STATIONS:
        return api_error('Choose a valid police station.', 400)
    if not identifier or not password:
        return api_error('Police ID or email and password are required.', 400)
    officer = PoliceOfficer.query.filter(
        db.func.lower(PoliceOfficer.police_id) == identifier.lower(),
        PoliceOfficer.is_active.is_(True),
    ).first()
    if not officer and '@' in identifier:
        user = User.query.filter_by(email=identifier.lower(), role='POLICE_OFFICER').first()
        officer = PoliceOfficer.query.filter_by(user_id=user.id, is_active=True).first() if user else None
    user = db.session.get(User, officer.user_id) if officer else None
    if not officer or not user or user.role != 'POLICE_OFFICER' or not check_password_hash(user.password_hash, password):
        return api_error('Police ID/email or password is incorrect.', 401)
    session.clear()
    session['user_id'] = user.id
    session['police_station'] = police_station
    return jsonify(user=user_json(user), police_id=officer.police_id, police_station=police_station)


@app.post('/api/police/logout')
def police_logout():
    session.clear()
    return jsonify(message='Signed out of the Police Portal.')


@app.post('/api/police/officers')
@require_auth({'ADMIN_DEMO'})
def create_police_officer():
    data = request.get_json(silent=True) or {}
    police_id = str(data.get('police_id') or '').strip()
    name = str(data.get('name') or '').strip()
    email = str(data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    phone = str(data.get('phone') or '').strip() or None
    if (not police_id or len(police_id) > 40 or not re.fullmatch(r'[A-Za-z0-9_-]{3,40}', police_id)
            or not name or len(name) > 120 or not EMAIL_PATTERN.fullmatch(email) or len(email) > 160):
        return api_error('Police ID, name, and email are required and must fit their field limits.')
    if len(password) < 8:
        return api_error('Police account passwords must contain at least 8 characters.')
    if User.query.filter_by(email=email).first() or PoliceOfficer.query.filter(
        db.func.lower(PoliceOfficer.police_id) == police_id.lower()
    ).first():
        return api_error('That email or Police ID is already registered.', 409)
    user = User(
        name=name, email=email, phone=phone,
        password_hash=generate_password_hash(password),
        role='POLICE_OFFICER',
    )
    db.session.add(user)
    try:
        db.session.flush()
        officer = PoliceOfficer(user_id=user.id, police_id=police_id)
        db.session.add(officer)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return api_error('That email or Police ID is already registered.', 409)
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception('Failed to create police officer account.')
        return api_error('The police account could not be saved.', 500)
    return jsonify(message='Police officer account created.', officer={
        'police_id': officer.police_id, 'name': user.name, 'email': user.email,
    }), 201


@app.post('/api/auth/login')
def login():
    d = request.get_json() or {}; email = str(d.get('email') or '').strip().lower(); u = User.query.filter_by(email=email).first()
    if not u or not check_password_hash(u.password_hash, d.get('password', '')):
        return api_error('Invalid email or password.', 401)
    if u.role == 'POLICE_OFFICER':
        return api_error('Please sign in through the Police Portal.', 403)
    session.clear()
    session['user_id'] = u.id; return jsonify(user=user_json(u))


@app.post('/api/auth/logout')
def logout():
    session.clear(); return jsonify(message='Signed out.')


@app.get('/api/users/me')
@require_auth()
def me():
    return jsonify(user=user_json(current_user()), police_station=session.get('police_station'))


@app.put('/api/users/me')
@require_auth()
def update_my_profile():
    user = current_user()
    data = request.get_json() or {}
    name = str(data.get('name') or '').strip()
    phone = str(data.get('phone') or '').strip()
    location = str(data.get('location') or '').strip()
    new_password = str(data.get('new_password') or '')
    current_password = str(data.get('current_password') or '')
    if not name or len(name) > 120:
        return api_error('Name is required and must be 120 characters or fewer.')
    if len(phone) > 30 or len(location) > 180:
        return api_error('Phone or location exceeds the allowed length.')
    if new_password:
        if len(new_password) < 8:
            return api_error('New password must contain at least 8 characters.')
        if not current_password or not check_password_hash(user.password_hash, current_password):
            return api_error('Enter your current password to change it.', 403)
        user.password_hash = generate_password_hash(new_password)
    user.name = name
    user.phone = phone or None
    user.location = location or None
    db.session.commit()
    return jsonify(message='Profile updated.', user=user_json(user))


@app.get('/api/push/public-key')
@require_auth()
def push_public_key():
    if not app.config.get('VAPID_PUBLIC_KEY') or not app.config.get('VAPID_PRIVATE_KEY'):
        return api_error('Phone notifications are not configured on the server yet.', 503)
    return jsonify(public_key=app.config['VAPID_PUBLIC_KEY'])


@app.post('/api/push/subscription')
@require_auth()
def save_push_subscription():
    if not app.config.get('VAPID_PUBLIC_KEY') or not app.config.get('VAPID_PRIVATE_KEY'):
        return api_error('Phone notifications are not configured on the server yet.', 503)
    subscription = request.get_json(silent=True) or {}
    if not isinstance(subscription, dict):
        return api_error('Invalid phone notification subscription.')
    endpoint = subscription.get('endpoint')
    keys = subscription.get('keys') or {}
    try:
        host = urlsplit(endpoint).hostname.lower() if isinstance(endpoint, str) and endpoint.startswith('https://') else ''
    except ValueError:
        host = ''
    allowed_hosts = {'fcm.googleapis.com', 'web.push.apple.com'}
    allowed_suffixes = ('.push.services.mozilla.com', '.notify.windows.com', '.push.samsungosp.com')
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 4096 or not (host in allowed_hosts or host.endswith(allowed_suffixes)) or not isinstance(keys, dict) or not isinstance(keys.get('p256dh'), str) or not isinstance(keys.get('auth'), str):
        return api_error('Invalid phone notification subscription.')
    endpoint_hash = hashlib.sha256(endpoint.encode('utf-8')).hexdigest()
    row = PushSubscription.query.filter_by(endpoint_hash=endpoint_hash).first()
    if row:
        row.user_id = current_user().id
        row.subscription_json = json.dumps(subscription)
    else:
        db.session.add(PushSubscription(user_id=current_user().id, endpoint_hash=endpoint_hash, subscription_json=json.dumps(subscription)))
    db.session.commit()
    return jsonify(message='Phone notifications enabled on this device.'), 201


@app.delete('/api/push/subscription')
@require_auth()
def delete_push_subscription():
    subscription = request.get_json(silent=True) or {}
    endpoint = subscription.get('endpoint') if isinstance(subscription, dict) else None
    if isinstance(endpoint, str):
        endpoint_hash = hashlib.sha256(endpoint.encode('utf-8')).hexdigest()
        row = PushSubscription.query.filter_by(endpoint_hash=endpoint_hash, user_id=current_user().id).first()
        if row:
            db.session.delete(row)
            db.session.commit()
    return jsonify(message='Phone notification subscription removed.')


@app.get('/webcam-search')
def webcam_search_page():
    return send_app_index()


def can_access_webcam_session(session_record):
    user = current_user()
    return bool(user and session_record.investigator_id == user.id)


def can_review_match(match):
    user = current_user()
    if not user:
        return False
    if user.role in {'INVESTIGATOR', 'ADMIN_DEMO'}:
        return True
    if user.role not in {'FAMILY_USER', 'CITIZEN', 'POLICE_OFFICER'} or match.source_type != 'WEBCAM':
        return False
    session_record = db.session.get(WebcamSession, match.source_id)
    return bool(session_record and can_access_webcam_session(session_record))


@app.post('/api/ai/webcam/start')
@require_auth({'FAMILY_USER', 'CITIZEN', 'INVESTIGATOR', 'ADMIN_DEMO', 'POLICE_OFFICER'})
def webcam_start():
    payload = request.get_json(silent=True) or {}
    session_record = WebcamSession(
        session_id=next_webcam_session_id(),
        investigator_id=current_user().id,
        location_name=(payload.get('location_name') or '').strip() or None,
        monitoring_description=(payload.get('monitoring_description') or '').strip() or None,
        status='ACTIVE',
    )
    db.session.add(session_record)
    db.session.commit()
    return jsonify(success=True, session={
        'id': session_record.id,
        'session_id': session_record.session_id,
        'investigator_id': session_record.investigator_id,
        'location_name': session_record.location_name,
        'monitoring_description': session_record.monitoring_description,
        'status': session_record.status,
        'started_at': session_record.started_at.isoformat(),
    })


@app.post('/api/ai/webcam/process-frame')
@require_auth({'FAMILY_USER', 'CITIZEN', 'INVESTIGATOR', 'ADMIN_DEMO', 'POLICE_OFFICER'})
def webcam_process_frame():
    session_id = (request.form.get('session_id') or '').strip() or (request.get_json(silent=True) or {}).get('session_id')
    if not session_id:
        return api_error('A valid webcam session ID is required.', 400)
    session_record = WebcamSession.query.filter_by(session_id=session_id).first()
    if not session_record:
        return api_error('Webcam session not found.', 404)
    if not can_access_webcam_session(session_record):
        return api_error('You do not have permission to access this webcam session.', 403)
    if session_record.status != 'ACTIVE':
        return api_error('This monitoring session is not active.', 409)
    if 'frame' not in request.files:
        return api_error('A webcam frame is required.', 400)
    frame_file = request.files['frame']
    if not frame_file.filename:
        return api_error('A valid frame file is required.', 400)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], f'webcam_{uuid.uuid4().hex}{os.path.splitext(secure_filename(frame_file.filename))[1].lower() or ".jpg"}')
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    frame_file.save(file_path)
    try:
        faces = detect_multiple_faces(file_path)
        assignments = assign_webcam_face_tracks(session_record.session_id, faces)
        face_results = []
        for index, (face, (face_id, cached_result)) in enumerate(zip(faces, assignments), start=1):
            raw_crop = None
            bbox = {'x': face['x'], 'y': face['y'], 'width': face['width'], 'height': face['height']}
            if cached_result and cached_result.get('reference_id'):
                cached_person = WebcamPerson.query.filter_by(reference_id=cached_result['reference_id']).first()
                if not cached_person or cached_person.status != 'ACTIVE':
                    cached_result = None
            if cached_result is not None:
                face_results.append({**cached_result, 'bbox': bbox, 'index': index, 'label': 'Face ' + str(index)})
                continue
            try:
                image = cv2.imread(file_path)
                if image is not None:
                    raw_crop = _expanded_face_crop(image, face)
            except Exception:
                raw_crop = None

            status = 'NO_RELEVANT_MATCH'
            evidence_message = 'AI Evidence Indicator: no relevant match in the current reference set.'
            similarity_score = None
            reference_id = None
            matched_person = None
            match_id = None
            ranked_candidates = []
            match_chart = None
            if raw_crop is not None and raw_crop.size:
                quality = _face_quality_metrics(raw_crop)
                if not quality['ok']:
                    status = 'INSUFFICIENT_QUALITY'
                    evidence_message = quality['reason']
                else:
                    embedding = _webcam_face_embedding(image, face)
                    matches = search_webcam_people(embedding, current_user().id)
                    if matches:
                        best = max(matches, key=lambda candidate: candidate['similarity'])
                        qualified_matches = [candidate for candidate in matches if candidate['cosine'] >= WEBCAM_FACE_COSINE_THRESHOLD]
                        status = 'POTENTIAL_MATCH' if qualified_matches else 'NO_RELEVANT_MATCH'
                        if status == 'POTENTIAL_MATCH':
                            matched_person = best['person']
                            similarity_score = float(best['similarity'])
                            reference_id = matched_person.reference_id
                            ranked_candidates = [{
                                'case': {
                                    **webcam_person_json(candidate['person']),
                                    'case_id': candidate['person'].reference_id,
                                },
                                'similarity_score': candidate['similarity'],
                                'status': 'WEBCAM_ONLY_REFERENCE',
                                'match_id': None,
                            } for candidate in qualified_matches]
                        evidence_message = 'AI Evidence Indicator: webcam-only reference lead requires human review.' if status == 'POTENTIAL_MATCH' else 'AI Evidence Indicator: no relevant match in the webcam-only reference set.'
                    else:
                        evidence_message = 'AI Evidence Indicator: no relevant match in the webcam-only reference set.'
            if status == 'POTENTIAL_MATCH' and reference_id:
                db.session.add(WebcamDetection(session_id=session_record.session_id, case_id=reference_id, similarity_score=similarity_score, frame_timestamp=datetime.utcnow().isoformat(), status=status))
            elif status in {'NO_RELEVANT_MATCH', 'INSUFFICIENT_QUALITY'}:
                db.session.add(WebcamDetection(session_id=session_record.session_id, frame_timestamp=datetime.utcnow().isoformat(), status=status))

            face_result = {
                'face_id': face_id,
                'bbox': bbox,
                'status': status,
                'evidence_message': evidence_message,
                'index': index,
                'label': 'Face ' + str(index),
                'case_id': None,
                'reference_id': reference_id,
                'similarity_score': similarity_score,
                'matched_person': webcam_person_json(matched_person) if matched_person and status == 'POTENTIAL_MATCH' else None,
                'match_id': match_id,
                'ranked_matches': ranked_candidates,
                'chart': match_chart,
            }
            cache_webcam_face_result(session_record.session_id, face_id, face_result)
            face_results.append(face_result)

        session_record.faces_processed = (session_record.faces_processed or 0) + len(faces)
        session_record.potential_matches = sum(1 for face in face_results if face['status'] == 'POTENTIAL_MATCH')
        db.session.commit()
        face_boxes = [
            {
                'face_id': face['face_id'],
                **face['bbox'],
                'status': face['status'],
                'label': face['label'],
            }
            for face in face_results
        ]
        results = [
            {'face_index': face['index'], 'face_id': face['face_id'], 'status': face['status'], 'evidence_message': face['evidence_message'], 'reference_id': face['reference_id'], 'similarity_score': face['similarity_score']}
            for face in face_results
        ]
        return jsonify(
            success=True,
            faces_detected=len(faces),
            processing='ACTIVE',
            faces=face_results,
            results=results,
            face_boxes=face_boxes,
            session_id=session_record.session_id,
            potential_matches=session_record.potential_matches,
            message='AI evidence scan completed.' if faces else 'No face detected in the current frame.',
        )
    except ValueError as exc:
        db.session.rollback()
        return api_error(str(exc), 400)
    finally:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError:
            app.logger.warning('Could not remove temporary webcam image: %s', file_path)


def _process_video_frame(session_id, user_id, frame):
    success, encoded = cv2.imencode('.jpg', frame)
    if not success:
        return None
    with app.test_request_context(
        '/api/ai/webcam/process-frame',
        method='POST',
        data={
            'session_id': session_id,
            'frame': (BytesIO(encoded.tobytes()), 'video-frame.jpg'),
        },
        content_type='multipart/form-data',
    ):
        session['user_id'] = user_id
        response = webcam_process_frame.__wrapped__()
        if isinstance(response, tuple):
            response = response[0]
        return response.get_json()


def _phone_webcam_capture_loop(state):
    """Drain the phone stream continuously so old buffered frames never pile up."""
    last_jpeg_at = 0.0
    while not state['stop'].is_set():
        ok, frame = state['capture'].read()
        if not ok or frame is None:
            if not state['stop'].is_set():
                state['error'] = 'The phone webcam stream stopped. Check IP Webcam on your phone and reconnect.'
            break
        now = time.monotonic()
        with state['lock']:
            state['latest_frame'] = frame
            state['sequence'] += 1
            sequence = state['sequence']
        if now - last_jpeg_at >= 1 / 12:
            encoded_ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if encoded_ok:
                with state['lock']:
                    state['latest_jpeg'] = encoded.tobytes()
                    state['jpeg_sequence'] = sequence
                last_jpeg_at = now


@app.get('/api/ai/webcam/phone-preview/<session_id>')
@require_auth({'FAMILY_USER', 'CITIZEN', 'INVESTIGATOR', 'ADMIN_DEMO', 'POLICE_OFFICER'})
def webcam_phone_preview(session_id):
    session_record = WebcamSession.query.filter_by(session_id=session_id).first()
    if not session_record:
        return api_error('Phone webcam session not found.', 404)
    if not can_access_webcam_session(session_record):
        return api_error('You do not have permission to access this phone webcam session.', 403)
    with remote_webcam_captures_lock:
        state = remote_webcam_captures.get(session_id)
    if not state:
        return api_error('Phone webcam stream is no longer available. Reconnect the phone.', 410)

    @stream_with_context
    def generate_frames():
        last_sequence = -1
        while not state['stop'].is_set():
            with state['lock']:
                jpeg = state['latest_jpeg']
                sequence = state['jpeg_sequence']
            if jpeg and sequence != last_sequence:
                last_sequence = sequence
                yield (b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
                       + str(len(jpeg)).encode('ascii') + b'\r\n\r\n' + jpeg + b'\r\n')
            elif state.get('error'):
                break
            time.sleep(1 / 24)

    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame', headers={
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'Pragma': 'no-cache',
        'X-Accel-Buffering': 'no',
    })


@app.post('/api/ai/webcam/connect-phone')
@require_auth({'FAMILY_USER', 'CITIZEN', 'INVESTIGATOR', 'ADMIN_DEMO', 'POLICE_OFFICER'})
def webcam_connect_phone():
    payload = request.get_json(silent=True) or {}
    stream_url = (payload.get('stream_url') or '').strip()
    try:
        parsed = urlsplit(stream_url)
        host = parsed.hostname or ''
        address = ipaddress.ip_address(host)
        parsed.port  # Validate any supplied port number.
    except (ValueError, TypeError):
        return api_error('Enter the IP Webcam address, such as http://192.168.1.25:8080/videofeed.', 400)
    if parsed.scheme.lower() not in {'http', 'https'} or not address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified or parsed.username or parsed.password:
        return api_error('For phone webcam connection, use its private Wi-Fi IP address and HTTP(S) video URL.', 400)

    video_path = parsed.path if parsed.path and parsed.path != '/' else '/videofeed'
    # IP Webcam normally serves the LAN stream over plain HTTP, even when its
    # home URL was copied into the field with an https:// prefix.
    schemes = ['http', 'https']
    capture = None
    for scheme in schemes:
        candidate_url = parsed._replace(scheme=scheme, path=video_path).geturl()
        try:
            candidate = cv2.VideoCapture(
                candidate_url,
                cv2.CAP_FFMPEG,
                [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000],
            )
        except (TypeError, cv2.error):
            candidate = cv2.VideoCapture(candidate_url)
        if candidate and candidate.isOpened():
            try:
                candidate.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except cv2.error:
                pass
            first_frame_ok, first_frame = candidate.read()
            if first_frame_ok:
                capture = candidate
                break
        if candidate:
            candidate.release()
    if not capture:
        return api_error('Could not read the phone video stream. Start IP Webcam, use its /videofeed URL over HTTP, and keep both devices on the same Wi-Fi.', 400)
    frame_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    frame_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

    session_record = WebcamSession(
        session_id=next_webcam_session_id(),
        investigator_id=current_user().id,
        monitoring_description='Phone IP Webcam live stream',
        status='ACTIVE',
    )
    try:
        db.session.add(session_record)
        db.session.commit()
    except Exception:
        capture.release()
        db.session.rollback()
        raise
    encoded_ok, encoded = cv2.imencode('.jpg', first_frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not encoded_ok:
        capture.release()
        session_record.status = 'STOPPED'
        session_record.ended_at = datetime.utcnow()
        db.session.commit()
        return api_error('Could not prepare the phone webcam preview.', 500)
    state = {
        'capture': capture,
        'stop': Event(),
        'lock': Lock(),
        'latest_frame': first_frame,
        'latest_jpeg': encoded.tobytes(),
        'sequence': 1,
        'jpeg_sequence': 1,
        'error': None,
    }
    worker = Thread(target=_phone_webcam_capture_loop, args=(state,), daemon=True)
    state['thread'] = worker
    with remote_webcam_captures_lock:
        remote_webcam_captures[session_record.session_id] = state
    worker.start()
    return jsonify(success=True, session_id=session_record.session_id, frame_width=frame_width, frame_height=frame_height, message='Phone webcam connected.')


@app.post('/api/ai/webcam/process-phone-frame')
@require_auth({'FAMILY_USER', 'CITIZEN', 'INVESTIGATOR', 'ADMIN_DEMO', 'POLICE_OFFICER'})
def webcam_process_phone_frame():
    payload = request.get_json(silent=True) or {}
    session_id = (payload.get('session_id') or '').strip()
    session_record = WebcamSession.query.filter_by(session_id=session_id).first() if session_id else None
    if not session_record:
        return api_error('Phone webcam session not found.', 404)
    if not can_access_webcam_session(session_record):
        return api_error('You do not have permission to access this phone webcam session.', 403)
    if session_record.status != 'ACTIVE':
        return api_error('This phone webcam session is no longer active.', 409)
    with remote_webcam_captures_lock:
        state = remote_webcam_captures.get(session_id)
    if not state:
        return api_error('The phone webcam stream stopped. Check IP Webcam on your phone and reconnect.', 503)
    with state['lock']:
        frame = state['latest_frame'].copy() if state['latest_frame'] is not None else None
        stream_error = state.get('error')
    if stream_error:
        return api_error(stream_error, 503)
    if frame is None:
        return api_error('The phone webcam stream stopped. Check IP Webcam on your phone and reconnect.', 503)
    result = _process_video_frame(session_id, current_user().id, frame)
    if not result:
        return api_error('Could not analyze the latest phone webcam frame.', 500)
    return jsonify(result)


def _scan_video_capture(capture, session_record, is_live_stream=False):
    user_id = current_user().id
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = frame_count / fps if fps > 0 and frame_count > 0 else 0
    max_seconds = max(10, int(os.environ.get('MAX_VIDEO_SCAN_SECONDS', '120')))
    max_samples = max(1, min(60, int(os.environ.get('MAX_VIDEO_SAMPLE_FRAMES', '60'))))
    if is_live_stream:
        positions = [None] * min(max_samples, 10)
    elif duration > 0:
        scan_duration = min(duration, max_seconds)
        sample_count = min(max_samples, max(1, int(scan_duration / 2) + 1))
        positions = [int((scan_duration * index / max(1, sample_count - 1)) * 1000) for index in range(sample_count)]
    elif fps > 0:
        # Some HTTP video sources omit frame-count metadata. Seek at regular
        # intervals instead of examining only the first two seconds.
        positions = [int(index * max_seconds * 1000 / max_samples) for index in range(max_samples)]
    else:
        positions = [None] * max_samples

    best_matches_by_reference = {}
    samples_processed = 0
    for timestamp_ms in positions:
        if timestamp_ms is not None:
            capture.set(cv2.CAP_PROP_POS_MSEC, timestamp_ms)
        elif is_live_stream and samples_processed:
            time.sleep(1)
        ok, frame = capture.read()
        if not ok or frame is None:
            if timestamp_ms is not None:
                continue
            break
        samples_processed += 1
        # Analyze each sampled image independently instead of reusing a cached
        # webcam track result from an earlier moment in the clip.
        with webcam_tracks_lock:
            webcam_tracks.pop(session_record.session_id, None)
        result = _process_video_frame(session_record.session_id, user_id, frame)
        if not result:
            continue
        for face in result.get('faces', []):
            for candidate in face.get('ranked_matches', []):
                score = float(candidate.get('similarity_score') or 0)
                person = candidate.get('case') or candidate.get('person') or {}
                reference_id = person.get('reference_id') or person.get('case_id')
                if not reference_id:
                    continue
                if reference_id not in best_matches_by_reference or score > best_matches_by_reference[reference_id]['similarity_score']:
                    best_matches_by_reference[reference_id] = {
                        **candidate,
                        'similarity_score': score,
                        'timestamp_seconds': round(timestamp_ms / 1000, 1) if timestamp_ms is not None else samples_processed - 1,
                        'face_bbox': face.get('bbox'),
                    }
    best_matches = sorted(best_matches_by_reference.values(), key=lambda match: match['similarity_score'], reverse=True)
    return {
        'best_match': best_matches[0] if best_matches else None,
        'best_matches': best_matches,
        'frames_analyzed': samples_processed,
    }


@app.post('/api/ai/webcam/process-video')
@require_auth({'FAMILY_USER', 'CITIZEN', 'INVESTIGATOR', 'ADMIN_DEMO', 'POLICE_OFFICER'})
def webcam_process_video():
    request.max_content_length = int(os.environ.get('MAX_VIDEO_UPLOAD_MB', '100')) * 1024 * 1024
    video_file = request.files.get('video')
    payload = request.get_json(silent=True) or {}
    stream_url = (request.form.get('stream_url') or payload.get('stream_url') or '').strip()
    if not video_file and not stream_url:
        return api_error('Upload a video file or provide a video/stream URL.', 400)

    session_record = WebcamSession(
        session_id=next_webcam_session_id(),
        investigator_id=current_user().id,
        monitoring_description='Uploaded video or video stream review',
        status='ACTIVE',
    )
    db.session.add(session_record)
    db.session.commit()

    capture = None
    temp_path = None
    try:
        if stream_url:
            try:
                parsed = urlsplit(stream_url)
            except ValueError:
                return api_error('Enter a valid video/stream URL.', 400)
            host = (parsed.hostname or '').lower()
            scheme = parsed.scheme.lower()
            if scheme not in {'http', 'https', 'rtsp', 'rtsps'} or not host:
                return api_error('Use a direct HTTP, HTTPS, RTSP, or RTSPS video/stream URL.', 400)
            try:
                port = parsed.port or {'http': 80, 'https': 443, 'rtsp': 554, 'rtsps': 322}[scheme]
                try:
                    resolved_addresses = {ipaddress.ip_address(host)}
                except ValueError:
                    resolved_addresses = {
                        ipaddress.ip_address(address[4][0])
                        for address in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                    }
            except (ValueError, OSError):
                return api_error('The video URL has an invalid or unreachable host.', 400)
            if not resolved_addresses or any(not address.is_global for address in resolved_addresses):
                return api_error('Video URLs must point to a publicly reachable host.', 403)
            try:
                capture = cv2.VideoCapture(
                    stream_url,
                    cv2.CAP_FFMPEG,
                    [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000],
                )
            except (TypeError, cv2.error):
                capture = cv2.VideoCapture(stream_url)
            # HTTP(S) links commonly point to seekable video files. Treating
            # those as live streams only scanned their first few seconds.
            # RTSP(S) sources are live streams; ordinary HTTP(S) files use the
            # duration/frame-count sampling path above.
            is_live_stream = scheme in {'rtsp', 'rtsps'}
        else:
            if not video_file.filename:
                return api_error('Choose a video file to scan.', 400)
            # Accept any filename extension; OpenCV/FFmpeg validates the actual
            # container and codec when it opens and reads the uploaded file.
            extension = os.path.splitext(secure_filename(video_file.filename))[1].lower() or '.mp4'
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            temp_path = os.path.join(app.config['UPLOAD_FOLDER'], f'webcam_video_{uuid.uuid4().hex}{extension}')
            video_file.save(temp_path)
            capture = cv2.VideoCapture(temp_path)
            is_live_stream = False

        if not capture or not capture.isOpened():
            return api_error('The video could not be decoded. Use a format and codec supported by this server\'s OpenCV/FFmpeg installation.', 400)
        scan = _scan_video_capture(capture, session_record, is_live_stream)
        if scan['frames_analyzed'] == 0:
            return api_error('No readable video frames were found. Try another video format or codec.', 400)
        session_record.status = 'STOPPED'
        session_record.ended_at = datetime.utcnow()
        db.session.commit()
        return jsonify(
            success=True,
            session_id=session_record.session_id,
            frames_analyzed=scan['frames_analyzed'],
            best_match=scan['best_match'],
            best_matches=scan['best_matches'],
            message='Video scan completed.' if scan['best_matches'] else 'No similar registered person was found in the sampled frames.',
        )
    except (ValueError, cv2.error) as exc:
        db.session.rollback()
        return api_error(str(exc) or 'The video could not be analyzed.', 400)
    finally:
        if capture is not None:
            capture.release()
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                app.logger.warning('Could not remove temporary webcam video: %s', temp_path)
        if session_record.status == 'ACTIVE':
            session_record.status = 'STOPPED'
            session_record.ended_at = datetime.utcnow()
            db.session.commit()


@app.post('/api/ai/webcam/stop')
@require_auth({'FAMILY_USER', 'CITIZEN', 'INVESTIGATOR', 'ADMIN_DEMO', 'POLICE_OFFICER'})
def webcam_stop():
    payload = request.get_json(silent=True) or {}
    session_id = (payload.get('session_id') or request.form.get('session_id') or '').strip()
    session_record = WebcamSession.query.filter_by(session_id=session_id).first() if session_id else None
    if not session_record:
        return api_error('Webcam session not found.', 404)
    if not can_access_webcam_session(session_record):
        return api_error('You do not have permission to access this webcam session.', 403)
    session_record.status = 'STOPPED'; session_record.ended_at = datetime.utcnow(); db.session.commit()
    with remote_webcam_captures_lock:
        remote_state = remote_webcam_captures.pop(session_record.session_id, None)
    if remote_state:
        remote_state['stop'].set()
        remote_state['capture'].release()
        worker = remote_state.get('thread')
        if worker and worker.is_alive():
            worker.join(timeout=1)
    with webcam_tracks_lock:
        webcam_tracks.pop(session_record.session_id, None)
    return jsonify(success=True, message='Webcam monitoring stopped.', session={'session_id': session_record.session_id, 'status': session_record.status})


@app.get('/api/ai/webcam/session/<session_id>')
@require_auth({'FAMILY_USER', 'CITIZEN', 'INVESTIGATOR', 'ADMIN_DEMO', 'POLICE_OFFICER'})
def webcam_session_details(session_id):
    session_record = WebcamSession.query.filter_by(session_id=session_id).first()
    if not session_record:
        return api_error('Webcam session not found.', 404)
    if not can_access_webcam_session(session_record):
        return api_error('You do not have permission to access this webcam session.', 403)
    return jsonify(success=True, session={
        'id': session_record.id,
        'session_id': session_record.session_id,
        'investigator_id': session_record.investigator_id,
        'location_name': session_record.location_name,
        'monitoring_description': session_record.monitoring_description,
        'status': session_record.status,
        'faces_processed': session_record.faces_processed,
        'potential_matches': session_record.potential_matches,
        'started_at': session_record.started_at.isoformat(),
        'ended_at': session_record.ended_at.isoformat() if session_record.ended_at else None,
    })


@app.get('/api/ai/webcam/session/<session_id>/matches')
@require_auth({'FAMILY_USER', 'CITIZEN', 'INVESTIGATOR', 'ADMIN_DEMO', 'POLICE_OFFICER'})
def webcam_session_matches(session_id):
    session_record = WebcamSession.query.filter_by(session_id=session_id).first()
    if not session_record:
        return api_error('Webcam session not found.', 404)
    if not can_access_webcam_session(session_record):
        return api_error('You do not have permission to access this webcam session.', 403)
    rows = WebcamDetection.query.filter_by(session_id=session_id).order_by(WebcamDetection.created_at.desc()).all()
    return jsonify(success=True, matches=[{
        'id': row.id,
        'case_id': row.case_id,
        'similarity_score': row.similarity_score,
        'status': row.status,
        'frame_timestamp': row.frame_timestamp,
        'created_at': row.created_at.isoformat(),
        'person': webcam_person_json(WebcamPerson.query.filter_by(reference_id=row.case_id).first())
            if row.case_id and WebcamPerson.query.filter_by(reference_id=row.case_id).first() else None,
    } for row in rows])


@app.post('/api/ai/webcam/session/<session_id>/review')
@require_auth({'FAMILY_USER', 'CITIZEN', 'INVESTIGATOR', 'ADMIN_DEMO', 'POLICE_OFFICER'})
def create_webcam_review(session_id):
    session_record = WebcamSession.query.filter_by(session_id=session_id).first()
    if not session_record:
        return api_error('Webcam session not found.', 404)
    if not can_access_webcam_session(session_record):
        return api_error('You do not have permission to request review for this webcam session.', 403)
    return api_error('Webcam-only references do not create or update missing-person case reviews.', 410)


@app.get('/api/missing-persons')
@require_auth()
def list_people():
    q = MissingPerson.query.filter_by(approval_status='APPROVED')
    term = request.args.get('q', '').strip()
    if term:
        q = q.filter(db.or_(MissingPerson.name.ilike(f'%{term}%'), MissingPerson.case_id.ilike(f'%{term}%'), MissingPerson.last_seen_location.ilike(f'%{term}%')))
    if request.args.get('status'):
        q = q.filter_by(status=request.args['status'])
    return jsonify(items=[person_json(p) for p in q.order_by(MissingPerson.created_at.desc()).all()])


@app.get('/api/police/missing-person-cases')
@require_auth({'POLICE_OFFICER', 'ADMIN_DEMO', 'INVESTIGATOR'})
def list_missing_person_reviews():
    rows = MissingPerson.query.filter_by(approval_status='PENDING_APPROVAL').order_by(MissingPerson.created_at.asc()).all()
    station = session.get('police_station')
    if current_user().role == 'POLICE_OFFICER' and station not in POLICE_STATIONS:
        return api_error('Sign in again and choose a police station.', 403)
    if current_user().role == 'POLICE_OFFICER':
        local_rows = [person for person in rows if missing_person_matches_station(person, station)]
        outside_count = len(rows) - len(local_rows)
        rows = local_rows
    else:
        outside_count = 0
    owners = {u.id: u for u in User.query.filter(User.id.in_([p.registered_by for p in rows])).all()} if rows else {}
    items = []
    for person in rows:
        item = person_json(person)
        owner = owners.get(person.registered_by)
        item['reporter_name'] = owner.name if owner else 'Unknown'
        item['reporter_contact'] = owner.phone if owner else None
        items.append(item)
    return jsonify(items=items, outside_station_count=outside_count, police_station=station)


@app.post('/api/police/missing-person-cases/request-wider-review')
@require_auth({'POLICE_OFFICER'})
def request_wider_missing_person_review():
    station = session.get('police_station')
    if station not in POLICE_STATIONS:
        return api_error('Sign in again and choose a police station.', 403)
    admins = User.query.filter_by(role='ADMIN_DEMO').all()
    for admin in admins:
        db.session.add(Notification(user_id=admin.id, title='Police station review request',
            message=f'{station} requests supervisor approval to review pending missing-person reports outside its local area.', type='case'))
    db.session.commit()
    return jsonify(message='A supervisor approval request was sent.' if admins else 'No supervisor account is available to receive the request.')


@app.put('/api/police/missing-person-cases/<int:pid>')
@require_auth({'POLICE_OFFICER', 'ADMIN_DEMO', 'INVESTIGATOR'})
def review_missing_person_case(pid):
    person = db.session.get(MissingPerson, pid)
    if not person:
        return api_error('Case not found.', 404)
    if current_user().role == 'POLICE_OFFICER':
        station = session.get('police_station')
        if station not in POLICE_STATIONS:
            return api_error('Sign in again and choose a police station.', 403)
        if not missing_person_matches_station(person, station):
            return api_error('This report is outside your station area. Request supervisor approval first.', 403)
    if person.approval_status != 'PENDING_APPROVAL':
        return api_error('This case has already been reviewed.', 409)
    data = request.get_json(silent=True) or {}
    decision = str(data.get('decision') or '').upper()
    if decision not in {'APPROVED', 'REJECTED'}:
        return api_error('Choose Approve or Reject.')
    person.approval_status = decision
    person.reviewed_by = current_user().id
    person.reviewed_at = datetime.utcnow()
    person.rejection_reason = (str(data.get('rejection_reason') or '').strip()[:2000] or None) if decision == 'REJECTED' else None
    if decision == 'APPROVED':
        person.status = 'ACTIVE'
    add_timeline(person.case_id, 'CASE_' + decision, f'Missing-person report {decision.lower()} by an authorized reviewer.' + (f' Reason: {person.rejection_reason}' if person.rejection_reason else ''), current_user().id)
    message = 'Your missing-person report was approved and is now visible on the Missing Persons page.' if decision == 'APPROVED' else 'Your missing-person report was not approved.' + (f' Reason: {person.rejection_reason}' if person.rejection_reason else '')
    db.session.add(Notification(user_id=person.registered_by, title='Case approved' if decision == 'APPROVED' else 'Case not approved', message=f'{person.case_id}: {message}', type='case'))
    db.session.commit()
    return jsonify(message='Case ' + decision.lower() + '.', person=person_json(person))


@app.get('/api/ai/webcam/references')
@require_auth({'FAMILY_USER', 'CITIZEN', 'INVESTIGATOR', 'ADMIN_DEMO', 'POLICE_OFFICER'})
def list_webcam_references():
    user = current_user()
    people = WebcamPerson.query.filter_by(
        status='ACTIVE', registered_by=user.id
    ).order_by(WebcamPerson.created_at.desc()).all()
    return jsonify(items=[{**webcam_person_json(person), 'can_delete': True} for person in people])


@app.post('/api/ai/webcam/references')
@require_auth({'FAMILY_USER', 'CITIZEN', 'INVESTIGATOR', 'ADMIN_DEMO', 'POLICE_OFFICER'})
def create_webcam_reference():
    photo = None
    try:
        photo = save_file(request.files.get('photo'))
        if not photo:
            return api_error('A clear face photo is required.')
        image_path = os.path.join(app.config['UPLOAD_FOLDER'], os.path.basename(photo))
        vector = webcam_embedding_from_file(image_path)
        reference_id = next_webcam_reference_id()
        person = WebcamPerson(
            reference_id=reference_id,
            registered_by=current_user().id,
            name=reference_id,
            photo=photo,
            embedding_vector=json.dumps(vector.tolist()),
            embedding_dimension=int(vector.size),
            model_name=WEBCAM_FACE_MODEL,
        )
        db.session.add(person)
        db.session.commit()
        return jsonify(message='Webcam-only reference registered.', person=webcam_person_json(person)), 201
    except ValueError as error:
        db.session.rollback()
        if photo:
            try:
                os.remove(os.path.join(app.config['UPLOAD_FOLDER'], os.path.basename(photo)))
            except OSError:
                app.logger.exception('Could not remove a rejected webcam reference photo.')
        return api_error(str(error))


@app.delete('/api/ai/webcam/references/<int:reference_id>')
@require_auth({'FAMILY_USER', 'CITIZEN', 'INVESTIGATOR', 'ADMIN_DEMO', 'POLICE_OFFICER'})
def delete_webcam_reference(reference_id):
    person = db.session.get(WebcamPerson, reference_id)
    if not person:
        return api_error('Webcam reference not found.', 404)
    user = current_user()
    if person.registered_by != user.id:
        return api_error('Only the account that registered this webcam reference can remove it.', 403)
    reference_id_text = person.reference_id
    photo_path = os.path.join(app.config['UPLOAD_FOLDER'], os.path.basename(person.photo or ''))
    person.status = 'INACTIVE'
    person.name = reference_id_text
    person.age = None
    person.gender = None
    person.description = None
    person.embedding_vector = '[]'
    person.embedding_dimension = 0
    person.model_name = 'DEACTIVATED'
    WebcamDetection.query.filter_by(case_id=reference_id_text).delete(synchronize_session=False)
    db.session.commit()
    invalidate_webcam_case_cache(reference_id_text)
    try:
        if photo_path and os.path.isfile(photo_path):
            os.remove(photo_path)
        person.photo = ''
        db.session.commit()
    except OSError:
        app.logger.exception('Could not erase the deactivated webcam reference photo %s.', reference_id_text)
        return api_error('The reference was deactivated and its face data was erased, but its photo file could not be removed. Check server file permissions.', 500)
    return jsonify(message='Webcam reference removed. Its photo, profile details, face data, and saved match records were erased.')


@app.post('/api/missing-persons')
@require_auth({'FAMILY_USER', 'INVESTIGATOR', 'ADMIN_DEMO', 'POLICE_OFFICER'})
def create_person():
    photo = None
    try:
        d = request.form
        submitted_name = re.sub(r'\s+', ' ', (d.get('name') or '').strip())
        if not submitted_name or not request.files.get('photo'):
            return api_error('Name and photograph are required.')
        normalized_name = re.sub(r'[\W_]', '', submitted_name.casefold())
        submitted_age = int(d['age']) if d.get('age') else None
        submitted_gender = (d.get('gender') or '').strip().casefold()
        owner = current_user()
        visible_for_duplicate_check = db.or_(
            MissingPerson.approval_status == 'APPROVED',
            MissingPerson.registered_by == owner.id,
        )
        for existing in MissingPerson.query.filter(visible_for_duplicate_check).all():
            existing_name = re.sub(r'[\W_]', '', re.sub(r'\s+', ' ', (existing.name or '').strip()).casefold())
            if existing_name != normalized_name:
                continue
            if submitted_age is not None and existing.age is not None and submitted_age != existing.age:
                continue
            existing_gender = (existing.gender or '').strip().casefold()
            if submitted_gender not in {'', 'not specified'} and existing_gender not in {'', 'not specified'} and submitted_gender != existing_gender:
                continue
            return jsonify(error=f'This person is already registered as case {existing.case_id}. No new case was created.'), 409

        photo = save_file(request.files.get('photo'))
        if not photo:
            return api_error('A valid photograph is required.')
        approval_status = 'PENDING_APPROVAL' if owner.role == 'FAMILY_USER' else 'APPROVED'
        p = MissingPerson(case_id=next_case_id(), registered_by=owner.id, name=submitted_name, age=submitted_age, gender=d.get('gender'), photo=photo,
                         description=d.get('description'), clothing_description=d.get('clothing_description'), last_seen_location=d.get('last_seen_location'),
                         last_seen_date=date.fromisoformat(d['last_seen_date']) if d.get('last_seen_date') else None, last_seen_time=d.get('last_seen_time'),
                         status='PENDING_APPROVAL' if approval_status == 'PENDING_APPROVAL' else 'ACTIVE',
                         approval_status=approval_status)
        db.session.add(p); db.session.flush(); vector = embedding_from_file(os.path.join(app.config['UPLOAD_FOLDER'], os.path.basename(photo)))
        db.session.add(FaceEmbedding(missing_person_id=p.id, embedding_vector=json.dumps(vector.tolist()), model_name='opencv-normalized-face-descriptor', embedding_dimension=len(vector)))
        add_timeline(p.case_id, 'CASE_REGISTERED', 'Missing-person case registered.', current_user().id)
        confirmation = 'Your missing-person report has been submitted successfully and is awaiting police verification.' if owner.role == 'FAMILY_USER' else 'Missing-person case created successfully.'
        db.session.add(Notification(user_id=owner.id, title='Case submitted' if approval_status == 'PENDING_APPROVAL' else 'Case created', message=f'{p.case_id}: {confirmation}', type='success'))
        db.session.commit(); return jsonify(message=confirmation, person=person_json(p)), 201
    except ValueError as e:
        db.session.rollback()
        if photo:
            try:
                os.remove(os.path.join(app.config['UPLOAD_FOLDER'], os.path.basename(photo)))
            except OSError:
                app.logger.exception('Could not remove a rejected missing-person photo.')
        return api_error(str(e))


@app.get('/api/missing-persons/<int:pid>')
@require_auth()
def get_person(pid):
    p = db.session.get(MissingPerson, pid)
    if not p:
        return api_error('Case not found.', 404)
    viewer = current_user()
    if p.approval_status != 'APPROVED' and not (viewer.role in {'POLICE_OFFICER', 'ADMIN_DEMO', 'INVESTIGATOR'} or (viewer.role == 'FAMILY_USER' and p.registered_by == viewer.id)):
        return api_error('Case not found.', 404)
    can_update_status = bool(
        p.approval_status == 'APPROVED' and viewer.role in {'ADMIN_DEMO', 'POLICE_OFFICER'}
    )
    can_add_notes = viewer.role in {'INVESTIGATOR', 'ADMIN_DEMO'}
    return jsonify(
        person=person_json(p),
        timeline=[{'event_type': x.event_type, 'description': x.description, 'created_at': x.created_at.isoformat()} for x in Timeline.query.filter_by(case_id=p.case_id).order_by(Timeline.created_at.desc())],
        notes=[{'body': x.body, 'created_at': x.created_at.isoformat()} for x in InvestigationNote.query.filter_by(case_id=p.case_id).all()],
        permissions={'can_update_status': can_update_status, 'can_add_notes': can_add_notes,
                     'is_owner': viewer.role == 'FAMILY_USER' and p.registered_by == viewer.id},
    )


@app.put('/api/missing-persons/<int:pid>')
@require_auth({'ADMIN_DEMO', 'POLICE_OFFICER'})
def update_person(pid):
    p = db.session.get(MissingPerson, pid); d = request.get_json() or {}
    if not p:
        return api_error('Case not found.', 404)
    if p.approval_status != 'APPROVED':
        return api_error('Case status can be updated only after police approval.', 409)
    if normalize_case_status(p.status) == 'CLOSED':
        return api_error('This case is closed and its status can no longer be changed.', 409)
    user = current_user()
    if d.get('status') not in CASE_STATUSES:
        return api_error('Invalid case status.')
    if user.role in {'FAMILY_USER', 'POLICE_OFFICER'} and normalize_case_status(d['status']) == 'PERSON_FOUND':
        return api_error('A person-found status requires authorized investigator verification.', 403)
    p.status = d['status']; add_timeline(p.case_id, 'STATUS_UPDATED', f'Case status updated to {display_case_status(p.status)}.', current_user().id)
    notify_case(p.case_id, 'Case status updated', f'{p.case_id} is now {display_case_status(p.status)}.')
    db.session.commit()
    if normalize_case_status(p.status) == 'CLOSED':
        invalidate_webcam_case_cache(p.case_id)
    return jsonify(person=person_json(p))


@app.post('/api/ai/search')
@require_auth()
def ai_search():
    try:
        photo = save_file(request.files.get('photo'))
        if not photo:
            return api_error('A photograph is required.')
        exclude_case_id = (request.form.get('exclude_case_id') or '').strip()
        matches = search_embedding(embedding_from_file(os.path.join(app.config['UPLOAD_FOLDER'], os.path.basename(photo))), 'SEARCH', None)
        matches = apply_exclusion(matches, exclude_case_id)
        db.session.commit()
        return jsonify(notice='AI-generated results are potential leads and require human verification.',
                       chart=match_chart_data(matches),
                       matches=[{'case': person_json(x['person']), 'similarity_score': x['similarity'], 'assessment': 'POTENTIAL MATCH', 'status': 'REQUIRES_HUMAN_VERIFICATION'} for x in matches])
    except ValueError as e:
        return api_error(str(e))


@app.get('/api/my-cases')
@require_auth({'FAMILY_USER'})
def my_cases():
    rows = MissingPerson.query.filter_by(registered_by=current_user().id).order_by(MissingPerson.created_at.desc()).all()
    return jsonify(items=[person_json(person) for person in rows])


@app.post('/api/my-cases/<case_id>/family-report')
@require_auth({'FAMILY_USER'})
def submit_family_report(case_id):
    case = MissingPerson.query.filter_by(case_id=case_id, registered_by=current_user().id).first()
    if not case:
        return api_error('Registered case not found.', 404)
    data = request.get_json(silent=True) or {}
    location = str(data.get('location_name') or '').strip()
    details = str(data.get('details') or '').strip()
    condition = str(data.get('condition') or '').strip()
    raw_date = str(data.get('found_date') or '').strip()
    raw_time = str(data.get('found_time') or '').strip()
    if not location or not details:
        return api_error('Location and report details are required.')
    if len(location) > 200 or len(condition) > 200 or len(details) > 5000:
        return api_error('Location and condition must be 200 characters or fewer; details must be 5,000 characters or fewer.')
    try:
        found_date = date.fromisoformat(raw_date) if raw_date else None
        found_time = datetime.strptime(raw_time, '%H:%M').strftime('%H:%M') if raw_time else None
    except ValueError:
        return api_error('Date or time is invalid.')
    report = FamilyReport(case_id=case.case_id, submitted_by=current_user().id, location_name=location,
                          found_date=found_date, found_time=found_time, condition=condition or None, details=details)
    db.session.add(report)
    db.session.add(Notification(user_id=current_user().id, title='Family report submitted',
        message=f'{case.case_id}: Your family report has been sent to police for review.', type='case'))
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception('Failed to save family report for case %s.', case.case_id)
        return api_error('Family report could not be submitted.', 500)
    return jsonify(message='Family report submitted to police.', report_id=report.id), 201


@app.get('/api/my-reports')
@require_auth()
def my_reports():
    user = current_user()
    items = []
    for report in Sighting.query.filter_by(reported_by=user.id).order_by(Sighting.reported_at.desc()).all():
        items.append({'kind':'Sighting','reference':report.case_id or '', 'status':report.status or 'SUBMITTED', 'summary':report.description or 'Sighting report submitted for review.', 'location':report.location_name, 'created_at':report.reported_at.isoformat() if report.reported_at else None})
    for report in FoundPerson.query.filter_by(reported_by=user.id).order_by(FoundPerson.created_at.desc()).all():
        items.append({'kind':'Found person','reference':f'Report #{report.id}', 'status':report.verification_status or report.status or 'PENDING_HUMAN_REVIEW', 'summary':report.description or 'Found-person report submitted for human review.', 'location':report.location_name, 'created_at':report.created_at.isoformat() if report.created_at else None})
    for person in MissingPerson.query.filter_by(registered_by=user.id).order_by(MissingPerson.created_at.desc()).all():
        items.append({'kind':'Missing person case','reference':person.case_id, 'status':person.status or 'ACTIVE', 'summary':f'Case registered for {person.name}.', 'location':person.last_seen_location, 'created_at':person.created_at.isoformat() if person.created_at else None})
    for report in EmergencyCase.query.filter_by(reporter_user_id=user.id).order_by(EmergencyCase.created_at.desc()).all():
        items.append({'kind':f'Emergency - {report.category.replace("_", " ")}', 'reference':report.case_id, 'status':report.status, 'summary':report.description, 'location':report.location_name, 'created_at':report.created_at.isoformat() if report.created_at else None})
    items.sort(key=lambda item:item['created_at'] or '', reverse=True)
    return jsonify(items=items)


@app.post('/api/sightings')
@require_auth({'CITIZEN', 'FAMILY_USER'})
def sighting():
    photo = None
    try:
        data = request.form
        case_id = (data.get('case_id') or '').strip().upper()
        case = MissingPerson.query.filter(db.func.upper(MissingPerson.case_id) == case_id).first() if case_id else None
        if not case or case.approval_status != 'APPROVED':
            return api_error('No missing-person case matches that ID. Enter the JR-YYYY-NNNN case ID shown in Missing persons.', 404)
        description = (data.get('description') or '').strip()
        location_name = (data.get('location_name') or '').strip()
        if not location_name:
            return api_error('A location is required for a sighting report.')
        if len(description) > 5000 or len(location_name) > 200:
            return api_error('Description must be 5,000 characters or fewer and location must be 200 characters or fewer.')
        raw_date = (data.get('sighting_date') or '').strip()
        raw_time = (data.get('sighting_time') or '').strip()
        try:
            sighting_date = date.fromisoformat(raw_date) if raw_date else None
            sighting_time = datetime.strptime(raw_time, '%H:%M').strftime('%H:%M') if raw_time else None
        except ValueError:
            return api_error('Sighting date or time is invalid.')
        photo = save_sighting_photo(request.files.get('photo'))
        report = Sighting(
            case_id=case.case_id,
            reported_by=current_user().id,
            photo=photo,
            description=description or None,
            location_name=location_name or None,
            sighting_date=sighting_date,
            sighting_time=sighting_time,
            reported_at=datetime.utcnow(),
            status='SUBMITTED',
            review_status='PENDING_REVIEW',
        )
        db.session.add(report)
        db.session.flush()
        db.session.add(Timeline(case_id=case.case_id, event_type='SIGHTING_REPORTED',
            description=f'Citizen submitted sighting report #{report.id} for authorized review.', created_by=current_user().id))
        db.session.add(Notification(
            user_id=case.registered_by,
            title='New sighting reported',
            message=f'A citizen submitted sighting report #{report.id} for {case.case_id}. Police review is pending.',
            type='sighting_report',
            sighting_id=report.id,
        ))
        db.session.commit()
        return jsonify(message='Sighting submitted for police review.', sighting_id=report.id, review_status=report.review_status), 201
    except ValueError as error:
        db.session.rollback()
        if photo:
            try:
                os.remove(os.path.join(app.config['UPLOAD_FOLDER'], photo))
            except OSError:
                app.logger.exception('Could not remove a rejected sighting photo.')
        return api_error(str(error))
    except SQLAlchemyError:
        db.session.rollback()
        if photo:
            try:
                os.remove(os.path.join(app.config['UPLOAD_FOLDER'], photo))
            except OSError:
                app.logger.exception('Could not remove a failed sighting photo.')
        app.logger.exception('Failed to save sighting report.')
        return api_error('Sighting could not be saved. Please try again.', 500)


@app.get('/api/sightings/<int:sighting_id>')
@require_auth()
def get_sighting(sighting_id):
    report = db.session.get(Sighting, sighting_id)
    user = current_user()
    if not report or not can_access_sighting(report, user):
        return api_error('Sighting report not found.', 404)
    return jsonify(report=sighting_json(report))


@app.get('/api/sightings/<int:sighting_id>/photo')
@require_auth()
def get_sighting_photo(sighting_id):
    report = db.session.get(Sighting, sighting_id)
    if not report or not report.photo or not can_access_sighting(report, current_user()):
        return api_error('Sighting photo not found.', 404)
    filename = os.path.basename(report.photo)
    if report.photo.startswith('sightings/'):
        return send_from_directory(os.path.join(app.config['UPLOAD_FOLDER'], 'sightings'), filename)
    # Legacy sighting photos were stored at the upload root; access is still checked above.
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.get('/api/police/sightings')
@require_auth({'POLICE_OFFICER', 'ADMIN_DEMO'})
def list_police_sightings():
    reports = Sighting.query.order_by(Sighting.reported_at.desc()).limit(200).all()
    return jsonify(items=[sighting_json(report) for report in reports])


@app.get('/api/police/case-updates')
@require_auth({'POLICE_OFFICER', 'ADMIN_DEMO'})
def list_police_case_updates():
    reports = FoundPerson.query.filter(FoundPerson.matched_case_id.isnot(None)).order_by(FoundPerson.created_at.desc()).limit(500).all()
    family_reports = FamilyReport.query.order_by(FamilyReport.created_at.desc()).limit(500).all()
    return jsonify(items=[{
        'id': report.id, 'case_id': report.matched_case_id,
        'description': report.description, 'location_name': report.location_name,
        'found_date': report.found_date.isoformat() if report.found_date else None,
        'verified_location': report.verified_location,
        'verified_at': report.verified_at.isoformat() if report.verified_at else None,
        'review_status': report.verification_status,
        'photo_url': f'/api/found-persons/{report.id}/photo' if report.photo and report.verification_status == 'VERIFIED_FOUND' else None,
        'kind': 'Found person', 'submitted_at': report.created_at.isoformat() if report.created_at else None,
    } for report in reports], family_reports=[{
        'id': report.id, 'case_id': report.case_id, 'created_at': report.created_at.isoformat() if report.created_at else None,
    } for report in family_reports])


@app.get('/api/police/family-reports/<int:report_id>')
@require_auth({'POLICE_OFFICER', 'ADMIN_DEMO'})
def get_police_family_report(report_id):
    report = db.session.get(FamilyReport, report_id)
    if not report:
        return api_error('Family report not found.', 404)
    case = MissingPerson.query.filter_by(case_id=report.case_id).first()
    return jsonify(report={
        'id': report.id, 'case_id': report.case_id,
        'person_name': case.name if case else 'Unknown',
        'location_name': report.location_name,
        'found_date': report.found_date.isoformat() if report.found_date else None,
        'found_time': report.found_time, 'condition': report.condition,
        'details': report.details,
        'created_at': report.created_at.isoformat() if report.created_at else None,
    })


@app.get('/api/police/cases/<case_id>/family-reports')
@require_auth({'POLICE_OFFICER', 'ADMIN_DEMO'})
def list_police_family_reports(case_id):
    case = MissingPerson.query.filter_by(case_id=case_id).first()
    if not case:
        return api_error('Registered case not found.', 404)
    reports = FamilyReport.query.filter_by(case_id=case.case_id).order_by(FamilyReport.created_at.desc()).all()
    return jsonify(case_id=case.case_id, person_name=case.name, items=[{
        'id': report.id, 'location_name': report.location_name,
        'found_date': report.found_date.isoformat() if report.found_date else None,
        'found_time': report.found_time, 'condition': report.condition,
        'details': report.details, 'created_at': report.created_at.isoformat() if report.created_at else None,
    } for report in reports])


@app.put('/api/police/sightings/<int:sighting_id>')
@require_auth({'POLICE_OFFICER', 'ADMIN_DEMO'})
def update_police_sighting(sighting_id):
    report = db.session.get(Sighting, sighting_id)
    if not report:
        return api_error('Sighting report not found.', 404)
    data = request.get_json(silent=True) or {}
    review_status = str(data.get('review_status') or '').strip().upper()
    allowed = {'PENDING_REVIEW', 'UNDER_REVIEW', 'REVIEWED', 'NOT_RELEVANT'}
    if review_status not in allowed:
        return api_error('Choose a valid police review status.')
    report.review_status = review_status
    case = MissingPerson.query.filter_by(case_id=report.case_id).first()
    if case:
        add_timeline(case.case_id, 'SIGHTING_REVIEW_UPDATED',
            f'Police review status for sighting report #{report.id} set to {review_status.replace("_", " ")}.', current_user().id)
        db.session.add(Notification(
            user_id=case.registered_by,
            title='Sighting review updated',
            message=f'Police review status for sighting report #{report.id} on {case.case_id}: {review_status.replace("_", " ")}. This is not a confirmed match.',
            type='sighting_report',
            sighting_id=report.id,
        ))
    db.session.commit()
    return jsonify(message='Police review status updated.', report=sighting_json(report))


@app.post('/api/found-persons')
@require_auth()
def found():
    try:
        d = request.form; photo = save_file(request.files.get('photo'))
        if not photo:
            return api_error('A photograph is required.')
        exclude_case_id = (d.get('exclude_case_id') or '').strip()
        report = FoundPerson(reported_by=current_user().id, photo=photo, description=d.get('description'), location_name=d.get('location_name'),
                             found_date=date.today())
        db.session.add(report); db.session.flush(); matches = search_embedding(embedding_from_file(os.path.join(app.config['UPLOAD_FOLDER'], os.path.basename(photo))), 'FOUND_PERSON', report.id)
        matches = apply_exclusion(matches, exclude_case_id)
        db.session.commit(); return jsonify(message='Found-person report submitted. Human verification is required before any alert is sent.', report_id=report.id,
                                           chart=match_chart_data(matches),
                                           matches=[{'case': person_json(x['person']), 'case_id': x['person'].case_id, 'name': x['person'].name, 'similarity_score': x['similarity']} for x in matches]), 201
    except ValueError as e:
        return api_error(str(e))


@app.post('/api/found-persons/<int:report_id>/verify')
@require_auth()
def verify_found_person(report_id):
    d = request.get_json() or {}
    report = db.session.get(FoundPerson, report_id)
    if not report:
        return api_error('Found-person report not found.', 404)
    case_id = d.get('case_id')
    case = db.session.query(MissingPerson).filter_by(case_id=case_id).first() if case_id else None
    verified = bool(d.get('verified'))
    if verified:
        location = d.get('location_name') or report.location_name
        if not location:
            return api_error('A location is required before sending the verification alert.')
        report.verification_status = 'VERIFIED_FOUND'; report.verified_found = True; report.verified_by = current_user().id; report.verified_at = datetime.utcnow(); report.verified_location = location; report.matched_case_id = case_id
        if case:
            case.status = 'PERSON_FOUND'; add_timeline(case.case_id, 'PERSON_FOUND_VERIFIED', f'Human verification confirmed the person was found at {location}.', current_user().id)
            message = f'A human verification confirmed that {case.name} was found near {location}. Please review the case and update follow-up actions.'
            db.session.add(Notification(user_id=case.registered_by, title='Confirmed found person', message=message,
                                        type='found_person', found_person_id=report.id))
            send_case_email(case.case_id, 'JanRaksha AI: person found and verified', message)
        db.session.commit(); return jsonify(message='Human verification confirmed. The alert was sent to the registered case owner.', report={'id': report.id, 'verification_status': report.verification_status, 'verified_location': report.verified_location}), 200
    report.verification_status = 'VERIFIED_NOT_FOUND'; report.verified_found = False; report.verified_by = current_user().id; report.verified_at = datetime.utcnow(); report.matched_case_id = case_id
    if case:
        add_timeline(case.case_id, 'FOUND_PERSON_REJECTED', 'Human verification rejected this found-person lead.', current_user().id)
    db.session.commit(); return jsonify(message='Human verification rejected this match. No alert was sent.', report={'id': report.id, 'verification_status': report.verification_status}), 200


@app.get('/api/found-persons/<int:report_id>')
@require_auth()
def get_found_person(report_id):
    report = db.session.get(FoundPerson, report_id)
    if (not report or report.verification_status != 'VERIFIED_FOUND'
            or not can_access_found_person(report, current_user())):
        return api_error('Found-person report not found.', 404)
    return jsonify(report={
        'id': report.id,
        'matched_case_id': report.matched_case_id,
        'description': report.description,
        'location_name': report.location_name,
        'found_date': report.found_date.isoformat() if report.found_date else None,
        'verified_location': report.verified_location,
        'verified_at': report.verified_at.isoformat() if report.verified_at else None,
        'photo_url': f'/api/found-persons/{report.id}/photo' if report.photo else None,
    })


@app.get('/api/found-persons/<int:report_id>/photo')
@require_auth()
def get_found_person_photo(report_id):
    report = db.session.get(FoundPerson, report_id)
    if (not report or report.verification_status != 'VERIFIED_FOUND' or not report.photo
            or not can_access_found_person(report, current_user())):
        return api_error('Found-person photo not found.', 404)
    return send_from_directory(app.config['UPLOAD_FOLDER'], os.path.basename(report.photo))


@app.post('/api/cases/<case_id>/notes')
@require_auth({'INVESTIGATOR', 'ADMIN_DEMO'})
def note(case_id):
    d = request.get_json() or {}
    if not d.get('body'):
        return api_error('Note text is required.')
    db.session.add(InvestigationNote(case_id=case_id, author_id=current_user().id, body=d['body']))
    add_timeline(case_id, 'INVESTIGATION_NOTE', 'Investigator added a restricted note.', current_user().id); db.session.commit(); return jsonify(message='Note added.'), 201


@app.get('/api/matches')
@require_auth({'INVESTIGATOR', 'ADMIN_DEMO'})
def matches():
    rows = AIMatch.query.join(MissingPerson, AIMatch.missing_person_id == MissingPerson.id).filter(
        MissingPerson.approval_status == 'APPROVED'
    ).order_by(AIMatch.created_at.desc()).all()
    return jsonify(items=[{'id': x.id, 'case_id': db.session.get(MissingPerson, x.missing_person_id).case_id, 'name': db.session.get(MissingPerson, x.missing_person_id).name,
                            'similarity_score': x.similarity_score, 'status': normalize_match_status(x.status), 'source_type': x.source_type,
                            'created_at': x.created_at.isoformat()} for x in rows])


@app.get('/api/matches/<int:match_id>')
@require_auth({'INVESTIGATOR', 'ADMIN_DEMO', 'FAMILY_USER', 'CITIZEN'})
def get_match(match_id):
    match = db.session.get(AIMatch, match_id)
    if not match:
        return api_error('Potential match not found.', 404)
    if not can_review_match(match):
        return api_error('You do not have permission to review this potential match.', 403)
    case = db.session.get(MissingPerson, match.missing_person_id)
    if not case or case.approval_status != 'APPROVED':
        return api_error('Associated missing-person case not found.', 404)
    report = None
    if match.source_type == 'FOUND_PERSON':
        report = db.session.get(FoundPerson, match.source_id)
    elif match.source_type == 'WEBCAM':
        report = db.session.get(WebcamSession, match.source_id)
    report_photo = report.photo if match.source_type == 'FOUND_PERSON' and report else None
    report_description = (
        report.monitoring_description if match.source_type == 'WEBCAM' and report
        else report.description if match.source_type == 'FOUND_PERSON' and report
        else None
    )
    report_location = report.location_name if report else None
    report_timestamp = (
        report.started_at if match.source_type == 'WEBCAM' and report
        else report.created_at if match.source_type == 'FOUND_PERSON' and report
        else None
    )
    return jsonify({
        'id': match.id,
        'status': normalize_match_status(match.status),
        'similarity_score': match.similarity_score,
        'combined_score': match.combined_score,
        'missing_person': person_json(case),
        'source_type': match.source_type,
        'source_id': match.source_id,
        'report': {
            'id': report.id if report else None,
            'photo': report_photo,
            'description': report_description,
            'location_name': report_location,
            'reported_at': report_timestamp.isoformat() if report_timestamp else None,
            'reporter': user_json(db.session.get(
                User,
                report.investigator_id if match.source_type == 'WEBCAM' else report.reported_by,
            )) if report and (
                (match.source_type == 'WEBCAM' and report.investigator_id)
                or (match.source_type == 'FOUND_PERSON' and report.reported_by)
            ) else None,
            'monitoring_description': report.monitoring_description if match.source_type == 'WEBCAM' and report else None,
            'session_id': report.session_id if match.source_type == 'WEBCAM' and report else None,
        },
    })


@app.post('/api/matches/<int:match_id>/verify')
@require_auth({'INVESTIGATOR', 'ADMIN_DEMO', 'FAMILY_USER', 'CITIZEN'})
def verify_match(match_id):
    payload = request.get_json() or {}
    decision = str(payload.get('decision', '')).upper()
    match = db.session.get(AIMatch, match_id)
    if not match:
        return api_error('Potential match not found.', 404)
    if not can_review_match(match):
        return api_error('You do not have permission to verify this potential match.', 403)
    match_case = db.session.get(MissingPerson, match.missing_person_id)
    if not match_case or match_case.approval_status != 'APPROVED':
        return api_error('Associated missing-person case not found.', 404)
    if normalize_match_status(match.status) == 'VERIFIED_BY_AUTHORIZED_USER' and decision == 'VERIFIED':
        case = match_case
        return jsonify(message='This match has already been verified.',
                       case={'case_id': case.case_id, 'status': normalize_case_status(case.status)} if case else None,
                       match={'id': match.id, 'status': normalize_match_status(match.status)}), 200
    if normalize_match_status(match.status) != 'REQUIRES_HUMAN_VERIFICATION':
        return api_error('This match is no longer awaiting human verification.', 409)
    if decision not in {'VERIFIED', 'NOT_RELEVANT'}:
        return api_error('Decision must be VERIFIED or NOT_RELEVANT.', 400)
    case = db.session.get(MissingPerson, match.missing_person_id)
    if not case or case.approval_status != 'APPROVED':
        return api_error('Associated missing-person case not found.', 404)
    actor = current_user()
    notes = (payload.get('notes') or '').strip()
    if decision == 'VERIFIED':
        if not notes:
            return api_error('Verification notes are required.', 400)
        match.status = 'VERIFIED_BY_AUTHORIZED_USER'
        case.status = 'PERSON_FOUND'
        add_timeline(case.case_id, 'POTENTIAL_MATCH_VERIFIED', 'Potential match verified by an authorized user. Case status updated to Person Found.', actor.id)
        add_audit_log(actor.id, case.case_id, match.id, 'MATCH_VERIFIED', {'decision': 'VERIFIED', 'notes': notes})
        recipient = db.session.get(User, case.registered_by)
        email_status = 'NO_REGISTERED_EMAIL'
        email_failure_reason = None
        if recipient:
            db.session.add(Notification(user_id=recipient.id, title='Important Case Update',
                                      message=f'Your missing-person case {case.case_id} has been updated to Person Found after human verification.',
                                      type='CASE_VERIFIED'))
            if recipient.email:
                notification = EmailNotification(case_id=case.case_id, match_id=match.id, recipient_user_id=recipient.id,
                                                 recipient_email=recipient.email, notification_type='VERIFIED_MATCH',
                                                 subject=f'JanRaksha AI — Important Update for Case {case.case_id}', status='PENDING', attempt_count=1)
                db.session.add(notification); db.session.flush()
                add_audit_log(actor.id, case.case_id, match.id, 'EMAIL_NOTIFICATION_CREATED', {'notification_id': notification.id, 'email': recipient.email})
                try:
                    send_verified_match_email(case.case_id, recipient.name, recipient.email)
                    notification.status = 'SENT'; notification.sent_at = datetime.utcnow(); notification.failure_reason = None
                    email_status = 'SENT'
                    add_audit_log(actor.id, case.case_id, match.id, 'EMAIL_SENT', {'notification_id': notification.id, 'email': recipient.email})
                except Exception as exc:
                    notification.status = 'FAILED'; notification.failure_reason = str(exc)[:500]
                    email_status = 'FAILED'
                    email_failure_reason = notification.failure_reason
                    add_audit_log(actor.id, case.case_id, match.id, 'EMAIL_FAILED', {'notification_id': notification.id, 'reason': str(exc)[:500]})
                    for admin in User.query.filter_by(role='ADMIN_DEMO').all():
                        db.session.add(Notification(user_id=admin.id, title='Email delivery failed',
                                                  message=f'Verified match email for case {case.case_id} could not be delivered to {recipient.email}.', type='warning'))
        db.session.commit()
        if email_status == 'SENT':
            message = f'Match verified successfully. Confirmation email sent to {recipient.email}.'
        elif email_status == 'NO_REGISTERED_EMAIL':
            message = 'Match verified successfully; no registered email address is available for email notification.'
        elif email_failure_reason == 'SMTP configuration missing.':
            message = 'Match verified successfully, but no email was sent: SMTP is not configured. Add your mail provider settings to the project .env file and restart the app.'
        else:
            message = f'Match verified successfully, but email delivery failed: {email_failure_reason or "check the app server log"}'
        return jsonify(message=message, case={'case_id': case.case_id, 'status': normalize_case_status(case.status)}, match={'id': match.id, 'status': normalize_match_status(match.status)}, email={'status': email_status, 'failure_reason': email_failure_reason}), 200

    match.status = 'NOT_RELEVANT'; add_timeline(case.case_id, 'AI_MATCH_REVIEWED', 'AI potential match reviewed and marked not relevant.', actor.id)
    add_timeline(case.case_id, 'MATCH_RECORDED', 'No person-found notification was sent because the lead was marked not relevant.', actor.id)
    add_audit_log(actor.id, case.case_id, match.id, 'MATCH_NOT_RELEVANT', {'decision': 'NOT_RELEVANT', 'notes': notes})
    db.session.commit(); return jsonify(message='Potential match marked not relevant. The case remains active.', match={'id': match.id, 'status': normalize_match_status(match.status)}), 200


@app.post('/api/matches/<int:match_id>/not-relevant')
@require_auth({'INVESTIGATOR', 'ADMIN_DEMO', 'FAMILY_USER', 'CITIZEN'})
def mark_not_relevant(match_id):
    payload = request.get_json() or {}
    notes = (payload.get('notes') or 'Marked not relevant by authorized investigator.').strip()
    match = db.session.get(AIMatch, match_id)
    if not match:
        return api_error('Potential match not found.', 404)
    if not can_review_match(match):
        return api_error('You do not have permission to review this potential match.', 403)
    if normalize_match_status(match.status) == 'VERIFIED_BY_AUTHORIZED_USER':
        return api_error('This verified match cannot be changed to not relevant.', 409)
    match.status = 'NOT_RELEVANT';
    case = db.session.get(MissingPerson, match.missing_person_id)
    if case and case.approval_status != 'APPROVED':
        return api_error('Associated missing-person case not found.', 404)
    if case:
        add_timeline(case.case_id, 'AI_MATCH_REVIEWED', 'AI potential match reviewed and marked not relevant.', current_user().id)
        add_timeline(case.case_id, 'MATCH_RECORDED', 'No person-found notification was sent because the lead was marked not relevant.', current_user().id)
    add_audit_log(current_user().id, case.case_id if case else None, match.id, 'MATCH_NOT_RELEVANT', {'notes': notes})
    db.session.commit(); return jsonify(message='Potential match marked not relevant.', match={'id': match.id, 'status': normalize_match_status(match.status)}), 200


@app.post('/api/notifications/email/<int:email_id>/retry')
@require_auth({'INVESTIGATOR', 'ADMIN_DEMO'})
def retry_email(email_id):
    email_record = db.session.get(EmailNotification, email_id)
    if not email_record:
        return api_error('Email record not found.', 404)
    if email_record.status == 'SENT':
        return api_error('This email has already been delivered successfully.', 409)
    if email_record.attempt_count >= 3:
        return api_error('This email exceeds the allowed retry limit.', 429)
    case = MissingPerson.query.filter_by(case_id=email_record.case_id).first()
    if not case:
        return api_error('Associated case no longer exists.', 404)
    user = db.session.get(User, email_record.recipient_user_id) or db.session.get(User, case.registered_by)
    if not user or not user.email:
        return api_error('Registered user email cannot be resolved.', 404)
    try:
        send_verified_match_email(case.case_id, user.name, user.email)
        email_record.status = 'SENT'; email_record.sent_at = datetime.utcnow(); email_record.failure_reason = None; email_record.attempt_count += 1
        add_audit_log(current_user().id, case.case_id, email_record.match_id, 'EMAIL_RETRY_SUCCESS', {'notification_id': email_record.id})
    except Exception as exc:
        email_record.status = 'FAILED'; email_record.failure_reason = str(exc)[:500]; email_record.attempt_count += 1
        add_audit_log(current_user().id, case.case_id, email_record.match_id, 'EMAIL_FAILED', {'notification_id': email_record.id, 'reason': str(exc)[:500]})
        admin_users = User.query.filter_by(role='ADMIN_DEMO').all()
        for admin in admin_users:
            db.session.add(Notification(user_id=admin.id, title='Retry email failed', message=f'Retry attempt for case {case.case_id} failed.', type='warning'))
    db.session.commit(); return jsonify(message='Email retry processed.', email={'id': email_record.id, 'status': email_record.status, 'attempt_count': email_record.attempt_count}), 200


@app.get('/api/notifications')
@require_auth()
def notifications():
    user = current_user()
    rows = Notification.query.filter_by(user_id=user.id).order_by(Notification.created_at.desc()).all()
    items = []
    for notification in rows:
        report = db.session.get(Sighting, notification.sighting_id) if notification.sighting_id else None
        sighting_id = report.id if report and can_access_sighting(report, user) else None
        items.append({
            'id': notification.id,
            'title': notification.title,
            'message': notification.message,
            'is_read': notification.is_read,
            'created_at': notification.created_at.isoformat(),
            'type': notification.type,
            'sighting_id': sighting_id,
            'found_person_id': notification.found_person_id if notification.found_person_id and can_access_found_person(
                db.session.get(FoundPerson, notification.found_person_id), user
            ) else None,
        })
    return jsonify(items=items)


@app.put('/api/notifications/<int:nid>/read')
@require_auth()
def read_notification(nid):
    n = db.session.get(Notification, nid)
    if not n or n.user_id != current_user().id:
        return api_error('Notification not found.', 404)
    n.is_read = True; db.session.commit(); return jsonify(message='Marked as read.')


@app.get('/api/dashboard/statistics')
@require_auth()
def stats():
    u = current_user(); cases = MissingPerson.query if u.role in {'INVESTIGATOR', 'ADMIN_DEMO'} else MissingPerson.query.filter_by(registered_by=u.id)
    people = cases.all(); ids = [x.case_id for x in people]
    return jsonify(active_cases=sum(normalize_case_status(x.status) == 'ACTIVE' for x in people), total_cases=len(people), new_sightings=Sighting.query.filter(Sighting.case_id.in_(ids)).count() if ids else 0,
                   potential_matches=AIMatch.query.join(MissingPerson).filter(MissingPerson.case_id.in_(ids)).count() if ids else 0,
                   notifications=Notification.query.filter_by(user_id=u.id, is_read=False).count())


@app.get('/api/emergency-contacts')
@require_auth()
def emergency_contacts():
    rows = EmergencyContact.query.filter_by(is_active=True).all(); return jsonify(items=[{'id': x.id, 'name': x.name, 'phone': x.phone, 'region': x.region, 'instructions': x.instructions} for x in rows])


@app.post('/api/emergency-contacts')
@require_auth({'ADMIN_DEMO'})
def add_emergency_contact():
    d = request.get_json() or {}
    if not d.get('name') or not d.get('phone'):
        return api_error('Name and phone are required.')
    row = EmergencyContact(name=d['name'], phone=d['phone'], region=d.get('region'), instructions=d.get('instructions')); db.session.add(row); db.session.commit(); return jsonify(message='Emergency contact saved.'), 201


@app.get('/api/trusted-contacts')
@require_auth()
def trusted_contacts():
    rows = TrustedContact.query.filter_by(user_id=current_user().id).all(); return jsonify(items=[{'id': x.id, 'name': x.name, 'email': x.email, 'phone': x.phone} for x in rows])


@app.post('/api/trusted-contacts')
@require_auth()
def add_trusted_contact():
    d = request.get_json() or {}
    if not d.get('name') or not (d.get('email') or d.get('phone')):
        return api_error('Name and an email address or phone number are required.')
    row = TrustedContact(user_id=current_user().id, name=d['name'], email=d.get('email'), phone=d.get('phone')); db.session.add(row); db.session.commit(); return jsonify(message='Trusted contact added.', contact={'id': row.id, 'name': row.name, 'email': row.email, 'phone': row.phone}), 201


@app.delete('/api/trusted-contacts/<int:contact_id>')
@require_auth()
def remove_trusted_contact(contact_id):
    row = db.session.get(TrustedContact, contact_id)
    if not row or row.user_id != current_user().id:
        return api_error('Trusted contact not found.', 404)
    db.session.delete(row); db.session.commit(); return jsonify(message='Trusted contact removed.')


@app.post('/api/emergency/sos')
@require_auth()
def activate_sos():
    d = request.get_json() or {}; user = current_user()
    event = EmergencyEvent(user_id=user.id, location_name=d.get('location_name'),
                          message=d.get('message') or 'I activated an SOS alert and need assistance.')
    db.session.add(event); db.session.flush()
    location = d.get('location_name') or 'Location unavailable'
    body = f'JanRaksha AI SOS from {user.name}. {event.message}\nLocation: {location}\nTime: {event.created_at.strftime("%Y-%m-%d %H:%M UTC")}\n\nContact emergency services if there is immediate danger.'
    contacts = TrustedContact.query.filter_by(user_id=user.id).all()
    recipients = {user.email.strip().lower(): user.email.strip()}
    for contact in contacts:
        if contact.email and contact.email.strip():
            address = contact.email.strip()
            recipients.setdefault(address.lower(), address)
    delivered = [address for address in recipients.values() if send_email(address, 'JanRaksha AI SOS alert', body)]
    delivery_summary = f'{len(delivered)} of {len(recipients)} email recipient(s)'
    notification = Notification(user_id=user.id, title='SOS activated', message=f'SOS event #{event.id} activated. Email delivered to {delivery_summary}.', type='emergency')
    db.session.add(notification)
    db.session.commit()
    push_user_ids = {user.id}
    contact_emails = {address.lower() for address in recipients.values() if address.lower() != user.email.lower()}
    if contact_emails:
        contact_accounts = User.query.filter(db.func.lower(User.email).in_(contact_emails)).with_entities(User.id).all()
        push_user_ids.update(user_id for (user_id,) in contact_accounts)
    push_result = send_web_push(
        push_user_ids,
        'JanRaksha emergency alert',
        'An emergency alert was activated. Open JanRaksha to view the details.',
    )
    if push_result['configured']:
        push_summary = f"phone notification sent to {push_result['sent']} of {push_result['total']} enabled device(s)"
    else:
        push_summary = 'phone notifications are not configured on this server'
    notification.message += f' Phone push: {push_summary}.'
    db.session.commit()
    message = f'SOS activated. Email sent to {delivery_summary}. {push_summary.capitalize()}.'
    if len(delivered) < len(recipients):
        message += ' Check SMTP settings and server logs for email failures.'
    return jsonify(message=message, event={'id': event.id, 'status': event.status, 'created_at': event.created_at.isoformat()}, email_deliveries=len(delivered), email_recipients=len(recipients), push_deliveries=push_result['sent'], push_subscriptions=push_result['total'], push_configured=push_result['configured']), 201


@app.put('/api/emergency/sos/<int:event_id>/cancel')
@require_auth()
def cancel_sos(event_id):
    event = db.session.get(EmergencyEvent, event_id)
    if not event or event.user_id != current_user().id:
        return api_error('Emergency event not found.', 404)
    if event.status != 'ACTIVE':
        return api_error('This SOS event is already closed.', 409)
    event.status = 'CANCELLED'; event.cancelled_at = datetime.utcnow(); db.session.add(Notification(user_id=current_user().id, title='SOS cancelled', message=f'SOS event #{event.id} was cancelled.', type='emergency')); db.session.commit(); return jsonify(message='SOS cancelled.')


@app.get('/api/emergency/history')
@require_auth()
def emergency_history():
    rows = EmergencyEvent.query.filter_by(user_id=current_user().id).order_by(EmergencyEvent.created_at.desc()).all(); return jsonify(items=[{'id': x.id, 'status': x.status, 'location_name': x.location_name, 'message': x.message, 'created_at': x.created_at.isoformat(), 'cancelled_at': x.cancelled_at.isoformat() if x.cancelled_at else None} for x in rows])


def emergency_case_duplicate_of(case):
    """Return the earliest likely same-incident report, if one exists."""
    location = re.sub(r'[^\w]', '', (case.location_name or '').casefold())
    if not location:
        return None
    current_words = set(re.findall(r'\w+', (case.description or '').casefold()))
    if not current_words:
        return None
    window_start = case.created_at - timedelta(hours=2)
    earlier_cases = EmergencyCase.query.filter(
        EmergencyCase.category == case.category,
        EmergencyCase.created_at < case.created_at,
        EmergencyCase.created_at >= window_start,
    ).order_by(EmergencyCase.created_at.asc(), EmergencyCase.id.asc()).all()
    ignored_words = {'a', 'an', 'the', 'at', 'in', 'on', 'near', 'of', 'to', 'and', 'is', 'was', 'there', 'incident', 'reported', 'happened'}
    current_words -= ignored_words
    for earlier in earlier_cases:
        earlier_location = re.sub(r'[^\w]', '', (earlier.location_name or '').casefold())
        if earlier_location != location:
            continue
        earlier_words = set(re.findall(r'\w+', (earlier.description or '').casefold())) - ignored_words
        if current_words == earlier_words and current_words:
            return earlier
        shared_words = current_words & earlier_words
        smaller_description_size = min(len(current_words), len(earlier_words))
        if len(shared_words) >= 2 and smaller_description_size and len(shared_words) / smaller_description_size >= 0.5:
            return earlier
    return None


def emergency_case_json(case):
    officer = db.session.get(User, case.assigned_officer_id) if case.assigned_officer_id else None
    duplicate_of = emergency_case_duplicate_of(case)
    return {
        'case_id': case.case_id,
        'duplicate_of_case_id': duplicate_of.case_id if duplicate_of else None,
        'category': case.category,
        'reporter_user_id': case.reporter_user_id,
        'reporter_name': case.reporter_name,
        'reporter_contact': case.reporter_contact,
        'description': case.description,
        'location_name': case.location_name,
        'attachment': f'/api/emergency/case-attachments/{case.case_id}' if case.attachment else None,
        'status': case.status,
        'assigned_officer_id': case.assigned_officer_id,
        'assigned_officer_name': officer.name if officer else None,
        'created_at': case.created_at.isoformat(),
        'updated_at': case.updated_at.isoformat(),
    }


def remove_emergency_attachment(attachment):
    if not attachment:
        return
    try:
        os.remove(os.path.join(app.config['UPLOAD_FOLDER'], attachment))
    except OSError:
        app.logger.exception('Could not remove an orphaned emergency attachment.')


@app.post('/api/emergency/cases')
@require_auth()
def create_emergency_case():
    user = current_user()
    category = str(request.form.get('category') or '').strip().upper()
    description = str(request.form.get('description') or '').strip()
    location_name = str(request.form.get('location_name') or '').strip()
    reporter_name = str(request.form.get('reporter_name') or user.name).strip()
    reporter_contact = str(request.form.get('reporter_contact') or user.phone or user.email).strip()
    submission_key = str(request.form.get('submission_key') or '').strip() or None
    if category not in EMERGENCY_CATEGORIES:
        return api_error('Choose a valid emergency category.')
    if not description or len(description) > 5000:
        return api_error('Incident description is required and must be 5,000 characters or fewer.')
    if not location_name or len(location_name) > 200:
        return api_error('Incident location is required and must be 200 characters or fewer.')
    if not reporter_name or len(reporter_name) > 120 or len(reporter_contact) > 160:
        return api_error('Reporter name or contact exceeds the allowed length.')
    if submission_key and len(submission_key) > 64:
        return api_error('Invalid submission token.')
    if submission_key:
        existing = EmergencyCase.query.filter_by(submission_key=submission_key).first()
        if existing:
            if existing.reporter_user_id != user.id:
                return api_error('Submission token has already been used.', 409)
            return jsonify(message='This emergency report was already received.', case=emergency_case_json(existing)), 200
    try:
        attachment = save_emergency_attachment(request.files.get('attachment'))
    except ValueError as error:
        return api_error(str(error))
    case = EmergencyCase(
        case_id=f'EM-{datetime.utcnow():%Y%m%d}-{uuid.uuid4().hex[:10].upper()}',
        submission_key=submission_key,
        category=category,
        reporter_user_id=user.id,
        reporter_name=reporter_name,
        reporter_contact=reporter_contact or None,
        description=description,
        location_name=location_name,
        attachment=attachment,
    )
    db.session.add(case)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        duplicate = EmergencyCase.query.filter_by(submission_key=submission_key).first() if submission_key else None
        if duplicate and duplicate.reporter_user_id == user.id:
            remove_emergency_attachment(attachment)
            return jsonify(message='This emergency report was already received.', case=emergency_case_json(duplicate)), 200
        remove_emergency_attachment(attachment)
        return api_error('Emergency report conflicts with an existing submission.', 409)
    except SQLAlchemyError:
        db.session.rollback()
        remove_emergency_attachment(attachment)
        app.logger.exception('Failed to save emergency case.')
        return api_error('Emergency report could not be saved. Please try again.', 500)
    duplicate_of = emergency_case_duplicate_of(case)
    message = f'This report was received as a duplicate of case {duplicate_of.case_id}.' if duplicate_of else 'Emergency report submitted successfully.'
    return jsonify(message=message, case=emergency_case_json(case)), 201


@app.get('/api/emergency/case-attachments/<case_id>')
@require_auth()
def get_emergency_attachment(case_id):
    case = EmergencyCase.query.filter_by(case_id=case_id).first()
    user = current_user()
    if not case or not case.attachment:
        return api_error('Emergency attachment not found.', 404)
    if user.role not in {'POLICE_OFFICER', 'ADMIN_DEMO'} and case.reporter_user_id != user.id:
        return api_error('You do not have permission to access this attachment.', 403)
    return send_from_directory(
        os.path.join(app.config['UPLOAD_FOLDER'], 'emergency-cases'),
        os.path.basename(case.attachment),
    )


@app.get('/api/emergency/cases/mine')
@require_auth()
def my_emergency_cases():
    cases = EmergencyCase.query.filter_by(
        reporter_user_id=current_user().id
    ).order_by(EmergencyCase.created_at.desc()).limit(50).all()
    return jsonify(items=[emergency_case_json(case) for case in cases])


@app.get('/api/police/cases')
@require_auth({'POLICE_OFFICER', 'ADMIN_DEMO'})
def list_police_cases():
    category = request.args.get('category', '').strip().upper()
    status = request.args.get('status', '').strip().upper()
    search = request.args.get('search', '').strip()
    from_date = request.args.get('from_date', '').strip()
    to_date = request.args.get('to_date', '').strip()
    if category and category not in EMERGENCY_CATEGORIES:
        return api_error('Invalid emergency category.')
    if status and status not in EMERGENCY_CASE_STATUSES:
        return api_error('Invalid case status.')
    try:
        start_date = date.fromisoformat(from_date) if from_date else None
        end_date = date.fromisoformat(to_date) if to_date else None
    except ValueError:
        return api_error('Dates must use YYYY-MM-DD format.')
    if start_date and end_date and start_date > end_date:
        return api_error('Start date must not be after end date.')
    query = EmergencyCase.query
    if category:
        query = query.filter_by(category=category)
    if status:
        query = query.filter_by(status=status)
    if search:
        pattern = f'%{search[:120]}%'
        query = query.filter(db.or_(
            EmergencyCase.case_id.ilike(pattern),
            EmergencyCase.description.ilike(pattern),
        ))
    if start_date:
        query = query.filter(EmergencyCase.created_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        query = query.filter(EmergencyCase.created_at < datetime.combine(end_date + timedelta(days=1), datetime.min.time()))
    try:
        page = max(1, int(request.args.get('page', '1')))
        per_page = min(100, max(1, int(request.args.get('per_page', '50'))))
    except ValueError:
        return api_error('Page and page size must be valid integers.')
    total = query.count()
    items = query.order_by(EmergencyCase.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
    counts = dict(db.session.query(EmergencyCase.status, db.func.count(EmergencyCase.id)).group_by(EmergencyCase.status).all())
    return jsonify(
        items=[emergency_case_json(case) for case in items],
        total=total,
        page=page,
        per_page=per_page,
        pages=(total + per_page - 1) // per_page,
        counts={
            'total': total,
            'pending': counts.get('PENDING', 0),
            'in_progress': counts.get('IN_PROGRESS', 0),
            'resolved': counts.get('RESOLVED', 0),
        },
        officers=[
            {'user_id': officer.user_id, 'police_id': officer.police_id, 'name': db.session.get(User, officer.user_id).name}
            for officer in PoliceOfficer.query.filter_by(is_active=True).order_by(PoliceOfficer.police_id).all()
        ],
    )


@app.get('/api/police/cases/<case_id>')
@require_auth({'POLICE_OFFICER', 'ADMIN_DEMO'})
def get_police_case(case_id):
    case = EmergencyCase.query.filter_by(case_id=case_id).first()
    if not case:
        return api_error('Emergency case not found.', 404)
    return jsonify(case=emergency_case_json(case))


@app.put('/api/police/cases/<case_id>')
@require_auth({'POLICE_OFFICER', 'ADMIN_DEMO'})
def update_police_case(case_id):
    case = EmergencyCase.query.filter_by(case_id=case_id).first()
    if not case:
        return api_error('Emergency case not found.', 404)
    data = request.get_json(silent=True) or {}
    status = str(data.get('status') or case.status).strip().upper()
    if status not in EMERGENCY_CASE_STATUSES:
        return api_error('Choose Pending, In Progress, or Resolved as the case status.')
    assigned_officer_id = data.get('assigned_officer_id', case.assigned_officer_id)
    if assigned_officer_id in ('', None):
        assigned_officer_id = None
    elif not isinstance(assigned_officer_id, int):
        return api_error('Assigned officer must be a valid officer account.')
    elif not PoliceOfficer.query.filter_by(user_id=assigned_officer_id, is_active=True).first():
        return api_error('Assigned officer must be an active police officer.', 400)
    case.status = status
    case.assigned_officer_id = assigned_officer_id
    case.updated_at = datetime.utcnow()
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception('Failed to update emergency case %s.', case_id)
        return api_error('Emergency case could not be updated.', 500)
    return jsonify(message='Emergency case updated.', case=emergency_case_json(case))


def backfill_sighting_notification_links():
    used_report_ids = {row[0] for row in db.session.query(Notification.sighting_id).filter(Notification.sighting_id.isnot(None)).all()}
    legacy = Notification.query.filter(
        Notification.title == 'New sighting reported',
        Notification.sighting_id.is_(None),
    ).all()
    for notification in legacy:
        match = re.search(r'JR-\d{4}-\d{4}', notification.message or '')
        if not match:
            continue
        case_id = match.group(0)
        case = MissingPerson.query.filter_by(case_id=case_id).first()
        if not case or case.registered_by != notification.user_id:
            continue
        reports = Sighting.query.filter_by(case_id=case_id).all()
        candidates = sorted(
            (report for report in reports if report.id not in used_report_ids and report.reported_at),
            key=lambda report: abs((report.reported_at - notification.created_at).total_seconds()),
        )
        if not candidates:
            continue
        distance = abs((candidates[0].reported_at - notification.created_at).total_seconds())
        if distance > 600:
            continue
        if len(candidates) > 1:
            next_distance = abs((candidates[1].reported_at - notification.created_at).total_seconds())
            if abs(next_distance - distance) < 1:
                continue
        notification.sighting_id = candidates[0].id
        used_report_ids.add(candidates[0].id)
    db.session.commit()


def ensure_sighting_schema():
    additions = {
        'missing_person': {
            'approval_status': "VARCHAR(30) NOT NULL DEFAULT 'PENDING_APPROVAL'",
            'reviewed_by': 'INTEGER',
            'reviewed_at': 'DATETIME',
            'rejection_reason': 'TEXT',
        },
        'sighting': {
            'sighting_date': 'DATE',
            'sighting_time': 'VARCHAR(10)',
            'review_status': "VARCHAR(30) NOT NULL DEFAULT 'PENDING_REVIEW'",
        },
        'notification': {'sighting_id': 'INTEGER', 'found_person_id': 'INTEGER'},
    }
    inspector = inspect(db.engine)
    for table, columns in additions.items():
        if not inspector.has_table(table):
            continue
        existing = {column['name'] for column in inspector.get_columns(table)}
        for column, definition in columns.items():
            if column not in existing:
                db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {definition}'))
    db.session.commit()
    backfill_sighting_notification_links()


@app.errorhandler(413)
def too_large(e):
    return api_error('File is too large.', 413)


@app.errorhandler(404)
def spa(e):
    if request.path.startswith('/api/'):
        return api_error('Resource not found.', 404)
    return send_app_index()


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        ensure_sighting_schema()
    app.run(debug=True, use_reloader=True)
